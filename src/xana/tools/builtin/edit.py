"""Edit: exact string replacement in a file.

Mirrors Claude Code's Edit semantics: old_string must appear exactly once
unless ``replace_all`` is true, in which case every occurrence is replaced.
A short unified diff is recorded in ToolResult.diffs for the harness's
audit trail.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import TYPE_CHECKING

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.builtin._reindex import maybe_reindex_markdown
from xana.tools.contracts import ToolDisplay, ToolContext, ToolDiff, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool

if TYPE_CHECKING:
    from xana.memory.markdown import MarkdownMemoryTree


_SPEC = ToolSpec(
    toolset="fs",
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
    display=ToolDisplay(category="edit", verb="修改", unit="个文件", summary_args=("file_path",)),
)


class EditTool(Tool):
    def __init__(
        self,
        *,
        memory_tree: MarkdownMemoryTree | None = None,
        protected_dir: "Path | tuple[Path, ...] | list[Path] | None" = None,
    ) -> None:
        # 语义同 WriteTool:md 增量 reindex + 受保护目录裸写门禁(§6.3 扩多目录)。
        self._memory_tree = memory_tree
        if protected_dir is None:
            self._protected_dirs: tuple[Path, ...] = ()
        elif isinstance(protected_dir, Path):
            self._protected_dirs = (protected_dir.resolve(),)
        else:
            self._protected_dirs = tuple(Path(d).resolve() for d in protected_dir)

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        for key in ("file_path", "old_string", "new_string"):
            if key not in invocation.input:
                raise ValueError(f"Edit requires {key!r}")
        if invocation.input["old_string"] == invocation.input["new_string"]:
            raise ValueError("Edit old_string must differ from new_string")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        target = resolve_path(invocation.input["file_path"], context.cwd).resolve()
        for protected in self._protected_dirs:
            try:
                target.relative_to(protected)
            except ValueError:
                continue
            return PermissionResult(
                decision="deny",
                reason=(
                    f"{target} 位于受保护目录,裸 Edit 被拒绝:"
                    "记忆(memory/)只能用 memory_write 写(action=create/update/delete),"
                    "agent 文档(AGENT.md)只能用 agent_define 写。"
                ),
            )
        return PermissionResult(decision="allow", reason="file edit allowed")

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
        if result.ok and self._memory_tree is not None:
            await maybe_reindex_markdown(
                self._memory_tree, invocation.input["file_path"], context.cwd
            )
        return result
