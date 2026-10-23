"""Glob: file pattern matching, sorted by modification time (newest first)."""
from __future__ import annotations

from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_LIMIT = 250


_SPEC = ToolSpec(
    temporal="static",
    toolset="fs",
    component=ComponentSpec(
        kind="tool",
        name="Glob",
        qualified_name="builtin.Glob",
        description="Match file paths against a glob pattern, newest first.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    },
    read_only=True,
    concurrency="parallel",
    display=ToolDisplay(category="search_code", verb="查找文件", summary_args=("pattern", "path")),
)


class GlobTool(Tool):
    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("pattern"), str) or not invocation.input["pattern"]:
            raise ValueError("Glob requires non-empty 'pattern'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only glob")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        pattern = invocation.input["pattern"]
        root = Path(invocation.input.get("path") or context.cwd)
        if not root.is_absolute():
            root = (Path(context.cwd) / root).resolve()
        if not root.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"path not found: {root}", failure_kind="not_found",
                rendered=f"[Glob failed: path not found: {root}]",
            )
        matches = sorted(
            (p for p in root.glob(pattern) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        truncated = len(matches) > _DEFAULT_LIMIT
        kept = matches[:_DEFAULT_LIMIT]
        rendered = "\n".join(str(p) for p in kept) if kept else "(no matches)"
        return ToolResult(
            ok=True,
            content=[str(p) for p in kept],
            rendered=rendered,
            truncated=truncated,
        )
