"""Grep: regex content search across files.

Two output modes mirroring CC: ``files_with_matches`` lists each matching
file path; ``content`` shows matching lines with optional line numbers.
A ``glob`` filter narrows which files are searched. No external ripgrep
dependency — pure Python ``re`` over filtered ``rglob``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_LIMIT = 250
_OutputMode = Literal["files_with_matches", "content"]


_SPEC = ToolSpec(
    temporal="static",
    toolset="fs",
    component=ComponentSpec(
        kind="tool",
        name="Grep",
        qualified_name="builtin.Grep",
        description="Regex search across files. Modes: files_with_matches (default), content.",
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

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        flags = re.IGNORECASE if invocation.input.get("case_insensitive") else 0
        try:
            regex = re.compile(invocation.input["pattern"], flags)
        except re.error as exc:
            return ToolResult(
                ok=False, content=None,
                error=f"invalid regex: {exc}", failure_kind="validation",
                rendered=f"[Grep failed: invalid regex: {exc}]",
            )

        root = Path(invocation.input.get("path") or context.cwd)
        if not root.is_absolute():
            root = (Path(context.cwd) / root).resolve()
        if not root.exists():
            return ToolResult(
                ok=False, content=None,
                error=f"path not found: {root}", failure_kind="not_found",
                rendered=f"[Grep failed: path not found: {root}]",
            )

        glob_pat = invocation.input.get("glob") or "**/*"
        files = (p for p in root.glob(glob_pat) if p.is_file())
        output_mode: _OutputMode = invocation.input.get("output_mode") or "files_with_matches"
        line_numbers = bool(invocation.input.get("line_numbers", True))

        if output_mode == "files_with_matches":
            return _search_files_with_matches(files, regex)
        return _search_content(files, regex, line_numbers)


def _search_files_with_matches(files, regex: re.Pattern) -> ToolResult:
    hits: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if regex.search(text):
            hits.append(str(path))
            if len(hits) >= _DEFAULT_LIMIT:
                break
    rendered = "\n".join(hits) if hits else "(no matches)"
    return ToolResult(ok=True, content=hits, rendered=rendered, truncated=len(hits) >= _DEFAULT_LIMIT)


def _search_content(files, regex: re.Pattern, line_numbers: bool) -> ToolResult:
    out: list[str] = []
    truncated = False
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                prefix = f"{path}:{lineno}:" if line_numbers else f"{path}:"
                out.append(f"{prefix}{line}")
                if len(out) >= _DEFAULT_LIMIT:
                    truncated = True
                    break
        if truncated:
            break
    rendered = "\n".join(out) if out else "(no matches)"
    return ToolResult(ok=True, content=out, rendered=rendered, truncated=truncated)
