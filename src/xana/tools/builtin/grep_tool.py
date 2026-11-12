"""Grep: regex content search across files.

Two output modes mirroring CC: ``files_with_matches`` lists each matching
file path; ``content`` shows matching lines with optional line numbers.
Python regex semantics are retained in a bounded, cancellable worker process.
Binary/oversized files are excluded explicitly; the service event loop never scans files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool
from xana.tools.builtin._process import ProcessRunner


_TIMEOUT_S = 30.0


_SPEC = ToolSpec(
    temporal="static",
    toolset="fs",
    component=ComponentSpec(
        kind="tool",
        name="Grep",
        qualified_name="builtin.Grep",
        description="Regex search text files up to 8 MiB; skips binary files. Modes: files_with_matches (default), content.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "output_mode": {"type": "string", "enum": ["files_with_matches", "content"]},
            "case_insensitive": {"type": "boolean", "default": False},
            "line_numbers": {"type": "boolean", "default": True},
        },
        "required": ["pattern"],
    },
    read_only=True,
    concurrency="parallel",
    display=ToolDisplay(category="search_code", verb="搜索代码", summary_args=("pattern", "path")),
)


class GrepTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("pattern"), str) or not invocation.input["pattern"]:
            raise ValueError("Grep requires non-empty 'pattern'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only grep")

    def __init__(self) -> None:
        self._runner = ProcessRunner()

    async def cancel(self, call_id: str) -> None:
        await self._runner.cancel(call_id)

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        root = Path(invocation.input.get("path") or context.cwd)
        if not root.is_absolute():
            root = (Path(context.cwd) / root).resolve()
        outcome = await self._runner.run(invocation.execution_id, argv=(
            sys.executable, str(Path(__file__).with_name("_grep_worker.py")),
            json.dumps({**invocation.input, "path": str(root)}, ensure_ascii=False)),
            cwd=context.cwd, timeout_s=_TIMEOUT_S)
        if outcome.timed_out or outcome.exit_code != 0:
            reason = "搜索超时，已终止工作进程，请缩小路径或 glob" if outcome.timed_out else "搜索工作进程失败"
            return ToolResult(ok=False, content=None, error=reason,
                              failure_kind="timeout" if outcome.timed_out else "execution",
                              rendered=f"[Grep failed: {reason}]")
        try:
            result = json.loads(outcome.output)
        except ValueError:
            return ToolResult(ok=False, content=None, error="invalid search worker response", failure_kind="execution")
        return ToolResult(**result)
