"""Read: file read tool with line numbers.

Mirrors Claude Code's Read tool surface: absolute path required (or
relative-to-cwd via the tool context), optional offset/limit for slicing
large files. Output is the file content prefixed with right-aligned line
numbers separated by a tab — the same format the model is trained on.
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_LIMIT = 2000


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="Read",
        qualified_name="builtin.Read",
        description="Read a file from the filesystem with line numbers.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["file_path"],
    },
    read_only=True,
    concurrency="parallel",
    display=ToolDisplay(category="read", verb="读取", unit="个文件", summary_args=("file_path",)),
)


class ReadTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("file_path"), str) or not invocation.input["file_path"]:
            raise ValueError("Read requires non-empty 'file_path'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only file access")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        path = resolve_path(invocation.input["file_path"], context.cwd)
        if not path.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"file not found: {path}", failure_kind="not_found",
                rendered=f"[Read failed: file not found: {path}]",
            )
        if path.is_dir():
            return ToolResult(
                ok=False, content=None,
                error=f"is a directory: {path}", failure_kind="validation",
                rendered=f"[Read failed: {path} is a directory]",
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = int(invocation.input.get("offset") or 0)
        limit = int(invocation.input.get("limit") or _DEFAULT_LIMIT)
        chunk = lines[offset : offset + limit]
        rendered = "\n".join(f"{offset + i + 1:>5}\t{line}" for i, line in enumerate(chunk))
        return ToolResult(ok=True, content=text, rendered=rendered)

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
