"""xana serve: boot adapters from system.json channels (env vars override), attach to gateway, run forever.

Config source: ~/.xana/config/system.json channels section.
Env vars override system.json values:
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN /
    FEISHU_ENCRYPT_KEY (optional) / FEISHU_CONNECTION_MODE ("ws" default |
    "webhook") / FEISHU_WEBHOOK_PORT (default 8765, webhook mode only)

Feishu inbound defaults to WebSocket long-connection mode (lark-oapi WS
client; no public HTTPS URL needed). The aiohttp webhook server is only
started when connection_mode == "webhook" (env FEISHU_CONNECTION_MODE or
system.json channels.feishu.connection_mode).
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS (csv)

A shared PairingStore (backed by pairings.json) is wired into both adapters
so `xana pair approve` hot-reloads into the running serve without restart.

Adapters whose required config is missing are skipped silently.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig
from xana.agent_adapter.interfaces import Adapter
from xana.config.loader import DefaultConfigProvider
from xana.config.system_model import SystemConfig
from xana.runtime.boot import boot
from xana.runtime.daemon_http import build_daemon_app, run_tcp, run_unix_socket


def build_adapters(
    *, bindings_root: Path, env: dict[str, str], system: SystemConfig,
    session_resolver: object = None,
) -> list[Adapter]:
    adapters: list[Adapter] = []
    pairing = PairingStore(path=bindings_root / "pairings.json")

    fs = system.channels.feishu
    fs_extra = fs.model_extra or {}
    app_id = env.get("FEISHU_APP_ID") or fs.app_id
    app_secret = env.get("FEISHU_APP_SECRET") or fs.app_secret_key
    verification_token = env.get("FEISHU_VERIFICATION_TOKEN") or str(
        fs_extra.get("verification_token") or ""
    )
    if app_id and app_secret:
        api_base = "https://open.larksuite.com" if fs.domain == "lark" else "https://open.feishu.cn"
        connection_mode = (
            env.get("FEISHU_CONNECTION_MODE")
            or str(fs_extra.get("connection_mode") or "")
            or "ws"
        ).strip().lower()
        if connection_mode not in ("ws", "webhook"):
            print(f"[feishu] unknown connection_mode '{connection_mode}' — falling back to ws")
            connection_mode = "ws"
        cfg = FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=env.get("FEISHU_ENCRYPT_KEY") or None,
            api_base=api_base,
            connection_mode=connection_mode,
        )
        bindings = BindingStore(path=bindings_root / "feishu.bindings.json")
        adapters.append(FeishuAdapter(
            config=cfg, bindings=bindings, pairing=pairing,
            session_resolver=session_resolver,
        ))

    tg = system.channels.telegram
    bot_token = env.get("TELEGRAM_BOT_TOKEN") or tg.bot_token
    if bot_token:
        env_allowed = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        allowed = (
            frozenset(int(x) for x in env_allowed.split(",") if x.strip())
            if env_allowed
            else frozenset(tg.allowed_user_ids)
        )
        cfg = TelegramConfig(bot_token=bot_token, allowed_user_ids=allowed)
        bindings = BindingStore(path=bindings_root / "telegram.bindings.json")
        adapters.append(TelegramAdapter(
            config=cfg, bindings=bindings, pairing=pairing,
            session_resolver=session_resolver,
        ))
    return adapters


def build_adapters_from_env(*, bindings_root: Path, env: dict[str, str]) -> list[Adapter]:
    """Backward-compat shim: delegates to build_adapters with an empty SystemConfig."""
    return build_adapters(bindings_root=bindings_root, env=env, system=SystemConfig())


def _make_scheduler_dispatch(gateway):
    """SchedulerRunner 的 dispatch 回调:把到点任务的 prompt 注入 gateway。

    入口选择:gateway.submit(InboundMessage) —— 与 adapter 入站泵同一条路径
    (router 在 gateway 内部把 InboundMessage 转成 AgentRequest,adapter.render
    把回复送回渠道)。adapter_id 按任务记录的 session_id 前缀推断
    (tg-… → telegram,feishu-… → feishu),没匹配时退回首个已注册 adapter。
    """
    prefix_to_adapter = {"tg": "telegram", "feishu": "feishu"}

    async def dispatch(*, session_id: str, text: str) -> None:
        from xana.agent_adapter.contracts import InboundMessage

        adapter_id = prefix_to_adapter.get(session_id.split("-", 1)[0])
        if adapter_id not in gateway.adapters:
            adapter_id = next(iter(gateway.adapters), None)
        if adapter_id is None:
            return
        inbound = InboundMessage(
            adapter_id=adapter_id, session_id=session_id, text=text,
            metadata={"source": "scheduler"},
        )
        async for _rendered in gateway.submit(inbound):
            pass

    return dispatch


async def _run_inbound_pump(adapter: Adapter, gateway) -> None:
    """Pull inbound messages, submit to gateway, and drain RenderedMessage stream.

    gateway.submit(inbound) already calls adapter.render(event) internally
    (see DefaultAgentGateway._submit) and yields RenderedMessage — so the pump
    just needs to drain that async generator to drive the full pipeline.

    每条 inbound 独立 task,让 gateway 的 detach 逻辑(P4)可以触发:
    第二条消息到来时,第一条 run 仍在跑——gateway 会把它转后台。
    串行 pump 会阻塞第二条直到第一条收尾,detach 永远没机会启动。
    """
    tasks: set[asyncio.Task] = set()

    async def _drain(stream) -> None:
        try:
            async for _ in stream:
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[pump] submit drain crashed: {e!r}")

    async for inbound in adapter.receive():
        t = asyncio.create_task(_drain(gateway.submit(inbound)))
        tasks.add(t)
        t.add_done_callback(tasks.discard)


def _xana_home() -> Path:
    override = os.environ.get("XANA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".xana"


_WORKSPACE_SEED = """# xana 助手

你是 xana,一个通过聊天渠道服务用户的个人助手。
"""


def _resolve_project_root(argv_project: Path | None) -> Path:
    """Resolve the memory/project root for serve.

    Explicit --project wins unchanged. Without it, default to
    <xana_home>/workspace (NOT cwd — running serve from the xana source repo
    must not mount the dev repo's README_ai.md as the bot's memory). The
    workspace dir is created on demand and seeded with a minimal
    README_ai.md if absent (never overwritten).
    """
    if argv_project is not None:
        return Path(argv_project).resolve()
    workspace = _xana_home() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    readme = workspace / "README_ai.md"
    if not readme.exists():
        readme.write_text(_WORKSPACE_SEED, encoding="utf-8")
    return workspace.resolve()


def _write_pidfile(home: Path) -> Path:
    """Record this process pid in <home>/run/serve.pid so `xana sync` can restart us."""
    pidfile = home / "run" / "serve.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    return pidfile


def _remove_pidfile(home: Path) -> None:
    pidfile = home / "run" / "serve.pid"
    try:
        if pidfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pidfile.unlink()
    except OSError:
        pass


def _make_daemon_on_action(gateway):
    """Build an on_action callback bridging daemon HTTP actions to gateway.

    button actions are handled client-side by the unified Card; server-side
    intervention re-enters via /submit as a /cmd text.
    """
    async def on_action(sid: str, kind: str, payload: dict) -> None:
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        if kind == "cancel":
            await gateway.cancel(sid, run_id)
        elif kind == "attach" and hasattr(gateway, "attach") and run_id:
            await gateway.attach(sid, run_id)
        elif kind == "detach" and hasattr(gateway, "detach") and run_id:
            await gateway.detach(sid, run_id)
        # button: client-managed, no-op
    return on_action


async def _start_daemon_in_process(
    *,
    project_root: Path,
    port: int | None,
    token: str,
    unix_socket: str | None = None,
):
    """boot + register DaemonBridgeAdapter + start HTTP server.

    Returns (bridge, gateway, runner_task). Cancel runner_task to shut down.
    Used by tests; serve_main uses the same building blocks inline.
    """
    boot_result = await boot(
        project_root=project_root,
        history_root=Path.home() / ".xana" / "sessions",
    )
    bridge = DaemonBridgeAdapter()
    await bridge.start()
    await boot_result.gateway.register_adapter("cli", bridge)

    on_action = _make_daemon_on_action(boot_result.gateway)
    app = build_daemon_app(bridge=bridge, token=token, on_action=on_action)

    if unix_socket:
        Path(unix_socket).parent.mkdir(parents=True, exist_ok=True)
        task = asyncio.create_task(run_unix_socket(app, unix_socket))
    else:
        task = asyncio.create_task(run_tcp(app, "127.0.0.1", port or 17777))
    return bridge, boot_result.gateway, task


async def _run(argv: argparse.Namespace) -> int:
    home = _xana_home()
    _write_pidfile(home)
    try:
        return await _run_inner(argv)
    finally:
        _remove_pidfile(home)


async def _run_inner(argv: argparse.Namespace) -> int:
    project_root = _resolve_project_root(argv.project)
    print(f"[xana serve] workspace {project_root}")
    from xana.cli.commands.pair import pairings_path
    cp = DefaultConfigProvider()
    boot_result = await boot(
        project_root=project_root,
        config_provider=cp,
        history_root=_xana_home() / "sessions",
    )
    system = cp.load_system_config()  # boot() already called cp.load()
    bindings_root = pairings_path().parent
    adapters = build_adapters(
        bindings_root=bindings_root, env=dict(os.environ), system=system,
        session_resolver=boot_result.session_resolver,
    )

    # Daemon HTTP+SSE wiring (always-on; replaces in-process xanac path).
    # Registered as adapter_id="cli" alongside feishu/tg adapters.
    daemon_cfg = system.daemon
    # TCP bind is MANDATORY — it's the OS-level single-instance lock. A
    # second `xana serve` invocation will fail this preflight before it
    # can steal the unix socket (UnixSite silently unlinks the old one,
    # so it can't be used as a guard). Pre-bind synchronously so the
    # error propagates back to main and exits the process cleanly.
    if not daemon_cfg.bind:
        raise RuntimeError(
            "daemon.bind must be set in system.json — TCP port is the "
            "single-instance lock; disabling it allows split-brain serves."
        )
    import socket as _socket
    _preflight = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _preflight.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _preflight.bind((daemon_cfg.bind, daemon_cfg.port))
    except OSError as e:
        _preflight.close()
        raise SystemExit(
            f"[xana serve] cannot bind {daemon_cfg.bind}:{daemon_cfg.port}"
            f" — another serve is already running ({e})"
        ) from None
    _preflight.close()

    daemon_bridge = DaemonBridgeAdapter(
        session_resolver=boot_result.session_resolver,
    )
    await daemon_bridge.start()
    await boot_result.gateway.register_adapter("cli", daemon_bridge)
    on_daemon_action = _make_daemon_on_action(boot_result.gateway)
    daemon_tasks: list[asyncio.Task] = []
    if daemon_cfg.unix_socket:
        unix_path = Path(daemon_cfg.unix_socket).expanduser()
        unix_path.parent.mkdir(parents=True, exist_ok=True)
        unix_app = build_daemon_app(
            bridge=daemon_bridge, token="", on_action=on_daemon_action,
        )
        daemon_tasks.append(
            asyncio.create_task(run_unix_socket(unix_app, str(unix_path)))
        )
        print(f"[daemon] unix socket {unix_path}")
    tcp_app = build_daemon_app(
        bridge=daemon_bridge, token=daemon_cfg.token, on_action=on_daemon_action,
    )
    daemon_tasks.append(
        asyncio.create_task(run_tcp(tcp_app, daemon_cfg.bind, daemon_cfg.port))
    )
    print(f"[daemon] tcp {daemon_cfg.bind}:{daemon_cfg.port}")
    if daemon_cfg.bind == "0.0.0.0":
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "daemon bound to 0.0.0.0 — exposed to LAN"
        )

    if not adapters:
        print("[xana serve] no chat adapters configured — daemon only.")

    pumps: list[asyncio.Task] = []
    webhook_runner = None

    # daemon_bridge needs an inbound pump just like feishu/tg adapters —
    # without it, /sessions/{sid}/submit POSTs land on bridge._inbound but
    # the gateway never reads them, so SSE clients hang forever waiting
    # for harness events.
    pumps.append(asyncio.create_task(
        _run_inbound_pump(daemon_bridge, boot_result.gateway)))

    for adapter in adapters:
        await adapter.start()
        await boot_result.gateway.register_adapter(adapter.adapter_id, adapter)
        if isinstance(adapter, FeishuAdapter):
            adapter.set_gateway(boot_result.gateway)
        if isinstance(adapter, TelegramAdapter) and hasattr(adapter, "set_gateway"):
            adapter.set_gateway(boot_result.gateway)
        pumps.append(asyncio.create_task(_run_inbound_pump(adapter, boot_result.gateway)))

        # Telegram「/」菜单:把可见命令推给 setMyCommands(飞书无此能力,依赖 /h)
        if isinstance(adapter, TelegramAdapter) and boot_result.gateway.command_router is not None:
            specs = await boot_result.gateway.command_router.list_commands()
            await adapter.sync_command_menu(
                [{"command": s.name, "description": s.description} for s in specs])

        if isinstance(adapter, FeishuAdapter):
            if adapter._config.connection_mode == "webhook":
                from aiohttp import web
                from xana.agent_adapter.channels.feishu_webhook import build_webhook_app
                app = build_webhook_app(config=adapter._config, on_event=adapter._on_event)
                webhook_runner = web.AppRunner(app)
                await webhook_runner.setup()
                port = int(os.environ.get("FEISHU_WEBHOOK_PORT", "8765"))
                site = web.TCPSite(webhook_runner, "0.0.0.0", port)
                await site.start()
                print(f"[feishu] webhook listening on :{port}/feishu/webhook")
            else:
                print("[feishu] websocket long-connection mode (no public URL needed)")

    runner = None
    if boot_result.scheduler is not None:
        from xana.runtime.scheduler import SchedulerRunner

        runner = SchedulerRunner(
            scheduler=boot_result.scheduler,
            dispatch=_make_scheduler_dispatch(boot_result.gateway),
        )
        runner.start()
        print("[xana serve] scheduler runner started")

    running_ids = ["cli", *(a.adapter_id for a in adapters)]
    print(f"[xana serve] running adapters: {', '.join(running_ids)}")

    # P5.6 — SIGTERM graceful drain: abort inflight runs, wait ≤2s, then exit.
    import signal as _signal
    from xana.cli.shutdown import graceful_shutdown as _graceful_shutdown
    _shutdown_evt = asyncio.Event()

    async def _on_sigterm() -> None:
        timed_out = await _graceful_shutdown(boot_result.gateway, grace_seconds=2.0)
        if timed_out:
            import logging as _logging
            _logging.warning(
                "graceful shutdown 2s 超时 — 强制退出,未完 run 下次启动按 shutdown_force 处理"
            )
        _shutdown_evt.set()

    try:
        asyncio.get_event_loop().add_signal_handler(
            _signal.SIGTERM, lambda: asyncio.create_task(_on_sigterm())
        )
    except (NotImplementedError, RuntimeError):
        pass  # platform without signal handler support (e.g. Windows)
    async def _run_pumps() -> None:
        if pumps:
            await asyncio.gather(*pumps)
        else:
            # daemon-only mode: keep alive until shutdown
            await asyncio.Event().wait()
    try:
        gather_pumps = asyncio.create_task(_run_pumps())
        shutdown_wait = asyncio.create_task(_shutdown_evt.wait())
        await asyncio.wait(
            {gather_pumps, shutdown_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if runner is not None:
            runner.stop()
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        for dt in daemon_tasks:
            dt.cancel()
        if daemon_tasks:
            await asyncio.gather(*daemon_tasks, return_exceptions=True)
        await daemon_bridge.stop()
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        for adapter in adapters:
            await adapter.stop()
        if boot_result.main_writer is not None:
            await boot_result.main_writer.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xana serve")
    p.add_argument("--project", type=Path, default=None)
    args = p.parse_args(argv)
    return asyncio.run(_run(args))
