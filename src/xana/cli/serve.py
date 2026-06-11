"""xana serve: boot adapters from env, attach to gateway, run forever.

Reads:
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN /
    FEISHU_ENCRYPT_KEY (optional) / FEISHU_WEBHOOK_PORT (default 8765)
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS (csv)

Adapters whose required env is missing are skipped silently.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agent_adapter.channels.telegram import TelegramAdapter, TelegramConfig
from xana.agent_adapter.interfaces import Adapter
from xana.runtime.boot import boot


def build_adapters_from_env(*, bindings_root: Path, env: dict[str, str]) -> list[Adapter]:
    adapters: list[Adapter] = []
    if all(env.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN")):
        cfg = FeishuConfig(
            app_id=env["FEISHU_APP_ID"],
            app_secret=env["FEISHU_APP_SECRET"],
            verification_token=env["FEISHU_VERIFICATION_TOKEN"],
            encrypt_key=env.get("FEISHU_ENCRYPT_KEY") or None,
        )
        bindings = BindingStore(path=bindings_root / "feishu.bindings.json")
        adapters.append(FeishuAdapter(config=cfg, bindings=bindings))
    if env.get("TELEGRAM_BOT_TOKEN"):
        allowed_csv = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        allowed = frozenset(int(x) for x in allowed_csv.split(",") if x.strip())
        cfg = TelegramConfig(bot_token=env["TELEGRAM_BOT_TOKEN"], allowed_user_ids=allowed)
        bindings = BindingStore(path=bindings_root / "telegram.bindings.json")
        adapters.append(TelegramAdapter(config=cfg, bindings=bindings))
    return adapters


async def _run_inbound_pump(adapter: Adapter, gateway) -> None:
    """Pull inbound messages, submit to gateway, and drain RenderedMessage stream.

    gateway.submit(inbound) already calls adapter.render(event) internally
    (see DefaultAgentGateway._submit) and yields RenderedMessage — so the pump
    just needs to drain that async generator to drive the full pipeline.
    """
    async for inbound in adapter.receive():
        async for _rendered in gateway.submit(inbound):
            pass  # gateway already called adapter.render(); nothing more to do


async def _run(argv: argparse.Namespace) -> int:
    project_root = (argv.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    bindings_root = Path.home() / ".xana" / "channels"
    adapters = build_adapters_from_env(bindings_root=bindings_root, env=dict(os.environ))
    if not adapters:
        print("[xana serve] no adapter env vars set — nothing to run.")
        return 1

    pumps: list[asyncio.Task] = []
    webhook_runner = None

    for adapter in adapters:
        await adapter.start()
        await boot_result.gateway.register_adapter(adapter.adapter_id, adapter)
        pumps.append(asyncio.create_task(_run_inbound_pump(adapter, boot_result.gateway)))

        if isinstance(adapter, FeishuAdapter):
            from aiohttp import web
            from xana.agent_adapter.channels.feishu_webhook import build_webhook_app
            app = build_webhook_app(config=adapter._config, on_event=adapter._on_event)
            webhook_runner = web.AppRunner(app)
            await webhook_runner.setup()
            port = int(os.environ.get("FEISHU_WEBHOOK_PORT", "8765"))
            site = web.TCPSite(webhook_runner, "0.0.0.0", port)
            await site.start()
            print(f"[feishu] webhook listening on :{port}/feishu/webhook")

    print(f"[xana serve] running adapters: {', '.join(a.adapter_id for a in adapters)}")
    try:
        await asyncio.gather(*pumps)
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        for adapter in adapters:
            await adapter.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xana serve")
    p.add_argument("--project", type=Path, default=None)
    args = p.parse_args(argv)
    return asyncio.run(_run(args))
