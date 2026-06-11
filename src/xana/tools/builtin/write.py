"""Write: create or overwrite a file with provided content."""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="Write",
        qualified_name="builtin.Write",
        description="Create a new file or overwrite an existing file with the given content.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    },
    destructive=True,
)


class WriteTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("file_path"), str) or not invocation.input["file_path"]:
            raise ValueError("Write requires non-empty 'file_path'")
        if "content" not in invocation.input:
            raise ValueError("Write requires 'content'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="file write allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        path = resolve_path(invocation.input["file_path"], context.cwd)
        content = str(invocation.input["content"])
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        action = "overwritten" if existed else "created"
        return ToolResult(
            ok=True,
            content=content,
            rendered=f"[Write ok] {action} {path} ({len(content)} bytes)",
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
