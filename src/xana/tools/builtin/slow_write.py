"""SlowWrite: TEMPORARY tool for P5 e2e cancel testing.

每秒 append 一行到 file_path,共 duration 秒。read_only=False(默认),
所以 cancel 时会等 2s 宽限。

用法:
  {"file_path": "/tmp/slow.txt", "duration": 10}

测试目的:
  - duration > 2 → cancel 后 2s 强杀,注入 P5.2 中文 warning,文件应停在第 2-3 秒。
  - duration < 2 → cancel 后正常完成,文件写满,无 warning。

⚠️ P5 验证完后删除本文件 + boot.py 里 register 行。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="SlowWrite",
        qualified_name="builtin.SlowWrite",
        description=(
            "[P5 测试用] 每秒 append 一行 '这是第 N 秒写的' 到 file_path,"
            "共 duration 秒。用于测 cancel 2s 宽限 + 强杀 warning。"
        ),
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "目标文件绝对路径"},
            "duration": {"type": "integer", "description": "总秒数(每秒写一行)"},
        },
        "required": ["file_path", "duration"],
    },
    destructive=True,
)


class SlowWriteTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("file_path"), str):
            raise ValueError("SlowWrite requires 'file_path'")
        if not isinstance(invocation.input.get("duration"), int):
            raise ValueError("SlowWrite requires int 'duration'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="slow_write test tool")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        path = Path(invocation.input["file_path"])
        duration = int(invocation.input["duration"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")  # truncate
        t0 = time.monotonic()
        for i in range(1, duration + 1):
            await asyncio.sleep(1.0)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"这是第 {i} 秒写的\n")
        elapsed = time.monotonic() - t0
        return ToolResult(
            ok=True,
            content=f"SlowWrite 完成: {duration} 秒, 实际 {elapsed:.2f}s, 文件 {path}",
            metadata={"duration": duration, "path": str(path)},
        )

    async def post_tool_use(
        self, invocation: ToolInvocation, context: ToolContext, result: ToolResult
    ) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or result.content or ""

    async def cancel(self, call_id: str) -> None:
        return None
