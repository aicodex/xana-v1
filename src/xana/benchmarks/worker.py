"""Headless Xana runtime. JSON-lines transport delegates tau tools to its runner.

No independent agent loop: every turn goes through boot() -> Agent.send().
Only this child process changes XANA_HOME; concurrent trials cannot mix state.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing, redirect_stdout
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, TextIO
from uuid import uuid4

from xana.agents.contracts import AgentRequest
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.extension_api.interfaces import ToolContributor
from xana.extension_api.registry import ExtensionRegistryBuilder
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.policy.contracts import PermissionResult
from xana.runtime.boot import BootResult, boot
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


class RemoteTools(ToolContributor):
    def __init__(self, schemas: list[dict], output: TextIO) -> None:
        self.output = output
        self.pending: dict[str, asyncio.Future] = {}
        self.instances = [RemoteTool(schema["function"], self) for schema in schemas]
        names = [tool.spec().component.name for tool in self.instances]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate benchmark tool names")

    def tools(self) -> list[Tool]:
        return list(self.instances)

    def emit(self, event: dict) -> None:
        self.output.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.output.flush()

    def resolve(self, result: dict) -> None:
        future = self.pending.get(result["id"])
        if future is None or future.done():
            raise ValueError(f"Unexpected tool result: {result['id']}")
        future.set_result(result)


class RemoteTool(Tool):
    def __init__(self, schema: dict, bridge: RemoteTools) -> None:
        self.bridge = bridge
        self.schema = schema

    def spec(self) -> ToolSpec:
        return ToolSpec(
            component=ComponentSpec(kind="tool", name=self.schema["name"],
                qualified_name="benchmark." + self.schema["name"],
                description=self.schema.get("description", ""),
                source=ComponentSource(source_type="builtin", scope="system")),
            input_schema=self.schema["parameters"], concurrency="serialized",
            interrupt_behavior="cancel",
        )

    async def check_permission(self, invocation: ToolInvocation,
                               context: ToolContext) -> PermissionResult:
        return PermissionResult("allow")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        future = asyncio.get_running_loop().create_future()
        self.bridge.pending[invocation.call_id] = future
        self.bridge.emit({"kind": "tool", "id": invocation.call_id,
                          "name": invocation.tool_name, "arguments": invocation.input})
        try:
            response = await future
            content = response.get("content") or ""
            error = response.get("error", False)
            return ToolResult(ok=not error, content=content, rendered=content,
                              error=content if error else None,
                              failure_kind="execution" if error else None)
        finally:
            self.bridge.pending.pop(invocation.call_id, None)


async def import_history(runtime: BootResult, messages: list[dict]) -> None:
    """Seed task-provided history once via the existing context lifecycle owner."""
    for msg in messages:
        role = msg["role"]
        if role not in {"assistant", "user", "tool"}:
            raise ValueError(f"Unsupported initial history role: {role}")
        metadata: dict[str, Any] = {}
        if msg.get("tool_calls"):
            metadata["tool_calls"] = [
                {"call_id": tc["id"], "tool_name": tc["name"], "input": tc["arguments"]}
                for tc in msg["tool_calls"]
            ]
        if role == "tool":
            metadata.update(tool_use_id=msg["id"], ok=not msg.get("error", False))
        row = HistoryRow(uuid4().hex, "trial", "benchmark", role,
                         MemoryMessage(role, msg.get("content") or "", metadata), time.time())
        await runtime.active_context_store.sdk().append_history(runtime.history_mirror, row)


async def run_turn(runtime: BootResult, text: str, bridge: RemoteTools) -> None:
    agent = runtime.agents["benchmark"]
    try:
        async with aclosing(agent.send(AgentRequest("trial", text, "benchmark"))) as events:
            async for event in events:
                if event.kind in {"error", "cancelled"}:
                    raise RuntimeError(str(event.payload))
                if event.kind == "final":
                    usage = event.harness_event.usage if event.harness_event else None
                    bridge.emit({"kind": "final", "text": event.payload.get("text", ""),
                                 "usage": asdict(usage) if usage else {}})
                    return
        raise RuntimeError("Xana exited without a final response")
    except Exception as exc:
        bridge.emit({"kind": "error", "error": str(exc)})


async def serve(home: Path, workspace: Path, source: TextIO, output: TextIO) -> None:
    os.environ["XANA_HOME"] = str(home.resolve())
    os.environ["XANA_PROJECT"] = "benchmark"
    init = json.loads(await asyncio.to_thread(source.readline))
    bridge = RemoteTools(init.get("tools", []), output)
    registry = ExtensionRegistryBuilder().add(bridge).build()
    runtime = await boot(project_root=workspace, extension_registry=registry,
                         history_root=home / "history", default_agent="benchmark")
    await import_history(runtime, init.get("history", []))
    bridge.emit({"kind": "ready"})
    task: asyncio.Task | None = None
    try:
        while line := await asyncio.to_thread(source.readline):
            request = json.loads(line)
            if request["kind"] == "user":
                if task is not None:
                    if not task.done():
                        raise ValueError("A user turn is still running")
                    await task
                task = asyncio.create_task(run_turn(runtime, request["text"], bridge))
            elif request["kind"] == "tool_result":
                bridge.resolve(request)
            else:
                raise ValueError(f"Unsupported transport message: {request['kind']}")
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    output = sys.stdout
    # Library diagnostics must not corrupt the transport.
    with redirect_stdout(sys.stderr):
        try:
            asyncio.run(serve(args.home, args.workspace, sys.stdin, output))
        except Exception as exc:
            output.write(json.dumps({"kind": "error", "error": str(exc)}) + "\n")
            output.flush()
            raise SystemExit(1)


if __name__ == "__main__":
    main()
