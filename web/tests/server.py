"""Isolated browser fixture: real harness/HTTP/storage, explicitly scripted fake model."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from dataclasses import replace

from aiohttp import web

from tests._model_config import select_fake_model
from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.cli.serve import _make_daemon_on_action, _run_inbound_pump
from xana.llm_driver.contracts import ModelEvent, ModelUsage
from xana.llm_driver.fake_client import FakeModelClient
from xana.runtime.boot import boot
from xana.runtime.daemon_http import build_daemon_app


class BrowserModel(FakeModelClient):
    async def capabilities(self, model=None):
        return replace(await super().capabilities(model), input_modalities=("image", "video"))

    async def _stream(self, request):
        users = [item.content for item in request.messages if item.role == "user" and not item.metadata.get("note_kind")]
        text = users[-1] if users else ""
        last_input = max((index for index, item in enumerate(request.messages)
                          if item.role == "user" and not item.metadata.get("note_kind")), default=-1)
        tool_last = any(item.role == "tool" for item in request.messages[last_input + 1:])
        if "child_work" in text:
            yield ModelEvent(kind="text_delta", payload={"text": "子任务正在执行"})
            await asyncio.sleep(40)
        elif "slow" in text:
            yield ModelEvent(kind="text_delta", payload={"text": "处理中"})
            await asyncio.sleep(40)
        elif not tool_last:
            tool = None
            if text == "write":
                tool = ("Write", {"file_path": "browser-approved.txt", "content": "approved"})
            elif text == "logs":
                tool = ("Bash", {"command": "echo browser-live-log; sleep 2; echo browser-log-done"})
            elif text == "delegate":
                tool = ("agent_run", {"prompt": "child_work", "wait_s": .01})
            elif text == "checklist":
                tool = ("TodoWrite", {"todos": [{"content": "浏览器验证", "active_form": "验证中", "status": "in_progress"}]})
            elif text == "goal-test":
                tool = ("update_goal", {"status": "complete"})
            if tool:
                yield ModelEvent(kind="tool_call", payload={"tool_name": tool[0], "input": tool[1], "call_id": "same-provider-call"})
                yield ModelEvent(kind="usage", usage=ModelUsage(input_tokens=10, output_tokens=5))
                yield ModelEvent(kind="done")
                return
        yield ModelEvent(kind="text_delta", payload={"text": "完成：" + text})
        yield ModelEvent(kind="usage", usage=ModelUsage(input_tokens=10, output_tokens=5))
        yield ModelEvent(kind="done")


async def main():
    with tempfile.TemporaryDirectory(prefix="xana-browser-") as directory:
        root = Path(directory)
        os.environ["XANA_HOME"] = str(root / "home")
        select_fake_model()
        runtime = await boot(project_root=root, history_root=root / "sessions")
        await runtime.model_registry.register(runtime.provider, BrowserModel())
        bridge = DaemonBridgeAdapter(session_resolver=runtime.session_resolver)
        await runtime.gateway.register_adapter("cli", bridge)
        app = build_daemon_app(bridge=bridge, token="browser-test-token", conversations=runtime.conversations,
            permissions=runtime.permissions, todos=runtime.todos, goals=runtime.goals,
            control=runtime.gateway.control, background=runtime.background, process_logs=runtime.process_logs,
            media_uploads=runtime.media_uploads,
            on_action=_make_daemon_on_action(runtime.gateway))
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 18899).start()
        pump = asyncio.create_task(_run_inbound_pump(bridge, runtime.gateway))
        print("browser fixture ready", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            pump.cancel()
            await runtime.gateway.cancel_all()
            await asyncio.gather(pump, return_exceptions=True)
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
