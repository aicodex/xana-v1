"""test_slow_task tool: blocks for delay_s seconds.

Heavy test tool only — never bundled into normal boot. Used by race_test/run.py
to force the model to make tool calls of controlled duration so we can isolate
the race-window timing from LLM round-trip latency.
"""
from __future__ import annotations

import asyncio
import time

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="test_slow_task",
        qualified_name="test.test_slow_task",
        description=(
            "测试用慢任务工具。按参数 delay_s 阻塞对应秒数后返回。"
            "用于多线程后台竞态测试。"
        ),
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "delay_s": {
                "type": "number",
                "description": "阻塞秒数。",
            },
            "label": {
                "type": "string",
                "description": "可选标签,会原样在结果里返回,方便日志识别。",
            },
        },
        "required": ["delay_s"],
    },
    read_only=True,
    concurrency="parallel",
    interrupt_behavior="cancel",
)


class SlowTaskTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        delay = invocation.input.get("delay_s")
        if delay is None:
            raise ValueError("delay_s required")
        try:
            float(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"delay_s must be numeric: {delay!r}") from exc

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="test tool")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        delay = float(invocation.input.get("delay_s", 0))
        label = str(invocation.input.get("label", ""))
        started = time.monotonic()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - started
            return ToolResult(
                ok=False,
                content=None,
                failure_kind="cancelled",
                error=f"cancelled after {elapsed:.2f}s",
                rendered=f"[test_slow_task {label!r} cancelled after {elapsed:.2f}s]",
            )
        elapsed = time.monotonic() - started
        body = {"delay_s": delay, "label": label, "elapsed_s": elapsed}
        return ToolResult(
            ok=True,
            content=body,
            rendered=f"[test_slow_task {label!r} done in {elapsed:.2f}s]",
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
