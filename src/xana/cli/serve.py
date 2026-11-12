"""xana serve: boot channel adapters, route messages through the gateway, run forever.

Config source: ~/.xana/config/system.json channels section.
Env vars override system.json values:
  FEISHU_APP_ID / FEISHU_APP_SECRET

Feishu inbound uses the WebSocket long connection (lark-oapi WS client; no
public HTTPS URL needed).
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

from xana.config.paths import project_home, project_id, project_root
from xana.config.project_config import daemon_socket
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig
from xana.agent_adapter.interfaces import Adapter
from xana.config.loader import DefaultConfigProvider
from xana.config.system_model import SystemConfig
from xana.runtime.boot import boot
from xana.multimodal.files import FileMediaStore
from xana.runtime.daemon_http import build_daemon_app, run_tcp, run_unix_socket
from xana.runtime.projects import serve_pidfile


def build_adapters(
    *, bindings_root: Path, env: dict[str, str], system: SystemConfig,
    session_resolver: object,
) -> list[Adapter]:
    adapters: list[Adapter] = []
    pairing = PairingStore(path=bindings_root / "pairings.json")

    fs = system.channels.feishu
    app_id = env.get("FEISHU_APP_ID") or fs.app_id
    app_secret = env.get("FEISHU_APP_SECRET") or fs.app_secret_key
    if app_id and app_secret:
        api_base = "https://open.larksuite.com" if fs.domain == "lark" else "https://open.feishu.cn"
        cfg = FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            api_base=api_base,
        )
        adapters.append(FeishuAdapter(
            config=cfg, pairing=pairing,
            media_store=FileMediaStore(project_home() / "sessions" / "media"),
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
        adapters.append(TelegramAdapter(
            config=cfg, pairing=pairing,
            session_resolver=session_resolver,
        ))
    return adapters


async def _run_inbound_pump(adapter: Adapter, gateway) -> None:
    """Pull inbound messages, submit to gateway, and drain RenderedMessage stream.

    gateway.submit(inbound) already calls adapter.render(event) internally
    (see DefaultAgentGateway._submit) and yields RenderedMessage — so the pump
    just needs to drain that async generator to drive the full pipeline.

    每条输入独立提交，使忙碌会话仍可接收补充消息。
    执行是否开始、何时消费补充，由共享 TurnCoordinator 决定。
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


def _resolve_project_root() -> Path:
    """当前项目(XANA_PROJECT,缺省 default)的项目目录,来自 projects/<id>/project.json
    (docs/design/project-design.md §3)。项目未注册即退出并提示。"""
    try:
        root = project_root()
    except LookupError as exc:
        raise SystemExit(f"[xana serve] {exc}") from None
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _acquire_instance_lock(home: Path):
    """单实例锁:进程一启动就对 <home>/run/serve.lock 加非阻塞排他 flock,持有到进程退出(OS 自动释放)。
    启动中的 serve 同样持有,所以前后脚启动的第二个 serve 会立刻退出,不会与第一个同时 boot、同时轮询渠道。"""
    import fcntl
    path = home / "run" / "serve.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise SystemExit("[xana serve] another serve is already running or starting (serve.lock held)") from None
    return fd


def _write_pidfile(pidfile: Path) -> None:
    """记下本进程 pid(runtime.projects.serve_pidfile),`xana sync` / `xana project` 据此找到并停止 serve。"""
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def _remove_pidfile(pidfile: Path) -> None:
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
        # button: client-managed, no-op
    return on_action


async def _start_daemon(boot_result, daemon_cfg) -> tuple[DaemonBridgeAdapter, list[asyncio.Task]]:
    """注册 xanac 用的 DaemonBridgeAdapter,并起 unix socket(无鉴权,本机)与 TCP(token 鉴权)两路 HTTP。"""
    daemon_bridge = DaemonBridgeAdapter(session_resolver=boot_result.session_resolver)
    await daemon_bridge.start()
    await boot_result.gateway.register_adapter("cli", daemon_bridge)
    on_daemon_action = _make_daemon_on_action(boot_result.gateway)
    tasks: list[asyncio.Task] = []
    # 每个项目一个 unix socket(未配置时取 projects/<id>/run/serve.sock)
    unix_path = daemon_socket(daemon_cfg)
    unix_path.parent.mkdir(parents=True, exist_ok=True)
    unix_app = build_daemon_app(bridge=daemon_bridge, token="", on_action=on_daemon_action,
                               conversations=boot_result.conversations, permissions=boot_result.permissions,
                               todos=boot_result.todos, goals=boot_result.goals, control=boot_result.gateway.control,
                               background=boot_result.background, process_logs=boot_result.process_logs,
                               media_uploads=boot_result.media_uploads)
    tasks.append(asyncio.create_task(run_unix_socket(unix_app, str(unix_path))))
    print(f"[daemon] unix socket {unix_path}")
    tcp_app = build_daemon_app(bridge=daemon_bridge, token=daemon_cfg.token, on_action=on_daemon_action,
                              conversations=boot_result.conversations, permissions=boot_result.permissions,
                              todos=boot_result.todos, goals=boot_result.goals, control=boot_result.gateway.control,
                              background=boot_result.background, process_logs=boot_result.process_logs,
                              media_uploads=boot_result.media_uploads)
    tasks.append(asyncio.create_task(run_tcp(tcp_app, daemon_cfg.bind, daemon_cfg.port)))
    print(f"[daemon] tcp {daemon_cfg.bind}:{daemon_cfg.port}")
    return daemon_bridge, tasks


async def _run(argv: argparse.Namespace) -> int:
    home = project_home()      # 锁、pid、socket 都按项目(一个项目一个 serve)
    lock = _acquire_instance_lock(home)
    # 拿到单实例锁即写 pidfile(启动中也能被 xana sync 找到并重启);拿不到锁的进程根本走不到这里,
    # 不会覆盖或删掉存活实例的 pidfile。
    pidfile = serve_pidfile(project_id())
    _write_pidfile(pidfile)
    try:
        return await _run_inner(argv)
    finally:
        _remove_pidfile(pidfile)  # 只删记录着本进程 pid 的 pidfile
        lock.close()


async def _run_inner(argv: argparse.Namespace) -> int:
    project_root = _resolve_project_root()
    print(f"[xana serve] project {project_id()} · workspace {project_root}")
    from xana.cli.commands.pair import pairings_path
    cp = DefaultConfigProvider()
    boot_result = await boot(
        project_root=project_root,
        config_provider=cp,
        default_agent=argv.agent,
        history_root=project_home() / "sessions",
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

    daemon_bridge, daemon_tasks = await _start_daemon(boot_result, daemon_cfg)
    if daemon_cfg.bind == "0.0.0.0":
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "daemon bound to 0.0.0.0 — exposed to LAN"
        )

    if not adapters:
        print("[xana serve] no chat adapters configured — daemon only.")

    pumps: list[asyncio.Task] = []

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
            print("[feishu] websocket long-connection mode (no public URL needed)")

    runner = None
    if boot_result.scheduler is not None:
        from xana.runtime.scheduler import SchedulerRunner

        # 到点任务在独立子会话里跑,结果通知插进该聊天当前的会话(docs/design/scheduled-task-design.md)
        runner = SchedulerRunner(
            scheduler=boot_result.scheduler,
            dispatch=boot_result.scheduler_dispatch,
        )
        runner.start()
        print("[xana serve] scheduler runner started")

    running_ids = ["cli", *(a.adapter_id for a in adapters)]
    print(f"[xana serve] running adapters: {', '.join(running_ids)}")
    background_notifications = None
    if boot_result.background is not None:
        # 渠道已起来:上次进程里被中断的后台任务通知父会话(不阻塞启动)
        background_notifications = asyncio.create_task(
            boot_result.background.retry_notifications(), name="xana-background-notifications")

    asyncio.create_task(boot_result.gateway.recover_inputs(), name="xana-input-recover")

    # SIGTERM graceful drain: abort inflight runs, wait ≤2s, then exit.
    import signal as _signal
    from xana.cli.shutdown import graceful_shutdown as _graceful_shutdown
    _shutdown_evt = asyncio.Event()

    async def _on_sigterm() -> None:
        timed_out = await _graceful_shutdown(boot_result.gateway, grace_seconds=30.0,
                                            scheduler_runner=runner)
        if timed_out:
            import logging as _logging
            _logging.warning(
                "graceful shutdown 30s 超时 — 强制退出,未完 run 下次启动按 shutdown_force 处理"
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
        if background_notifications is not None:
            background_notifications.cancel()
            await asyncio.gather(background_notifications, return_exceptions=True)
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
        for adapter in adapters:
            await adapter.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xana serve")
    p.add_argument("--project", default=None, help="项目 id(缺省 default;见 xana project list)")
    p.add_argument("--agent", default="chat", help="默认入口 agent")
    args = p.parse_args(argv)
    if args.project:
        os.environ["XANA_PROJECT"] = args.project   # 本进程内所有项目路径由此解析
    return asyncio.run(_run(args))
