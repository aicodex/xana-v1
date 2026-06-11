"""Edit: exact string replacement in a file.

Mirrors Claude Code's Edit semantics: old_string must appear exactly once
unless ``replace_all`` is true, in which case every occurrence is replaced.
A short unified diff is recorded in ToolResult.diffs for the harness's
audit trail.
"""
from __future__ import annotations

import difflib

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.contracts import ToolContext, ToolDiff, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="Edit",
        qualified_name="builtin.Edit",
        description="Replace an exact string in an existing file.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    destructive=True,
)


class EditTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        for key in ("file_path", "old_string", "new_string"):
            if key not in invocation.input:
                raise ValueError(f"Edit requires {key!r}")
        if invocation.input["old_string"] == invocation.input["new_string"]:
            raise ValueError("Edit old_string must differ from new_string")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="file edit allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        path = resolve_path(invocation.input["file_path"], context.cwd)
        if not path.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"file not found: {path}", failure_kind="not_found",
                rendered=f"[Edit failed: file not found: {path}]",
            )
        old = invocation.input["old_string"]
        new = invocation.input["new_string"]
        replace_all = bool(invocation.input.get("replace_all", False))

        original = path.read_text(encoding="utf-8")
        if old not in original:
            return ToolResult(
                ok=False, content=None,
                error=f"old_string not found in {path}",
                failure_kind="validation",
                rendered=f"[Edit failed: old_string not found in {path}]",
            )
        occurrences = original.count(old)
        if not replace_all and occurrences > 1:
            return ToolResult(
                ok=False, content=None,
                error=f"old_string appears {occurrences} times; set replace_all=true or add context",
                failure_kind="validation",
                rendered=f"[Edit failed: old_string not unique ({occurrences} matches)]",
            )
        replaced_count = occurrences if replace_all else 1
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        diff_text = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
                n=2,
            )
        )
        rendered = f"[Edit ok] {path} ({replaced_count} replacement{'s' if replaced_count != 1 else ''})"
        return ToolResult(
            ok=True,
            content=updated,
            rendered=rendered,
            diffs=(ToolDiff(path=path, diff=diff_text),),
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
