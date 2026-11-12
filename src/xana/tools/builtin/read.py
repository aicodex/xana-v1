"""Read: file read tool with line numbers.

Mirrors Claude Code's Read tool surface: absolute path required (or
relative-to-cwd via the tool context), optional offset/limit for slicing
large files. Output is the file content prefixed with right-aligned line
numbers separated by a tab — the same format the model is trained on.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from xana.multimodal.files import detect_format
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._path import resolve_path
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_LIMIT = 2000


_SPEC = ToolSpec(
    temporal="static",
    toolset="fs",
    component=ComponentSpec(
        kind="tool",
        name="Read",
        qualified_name="builtin.Read",
        description=("读取 UTF-8 文本文件(带行号，最大 8 MiB)。图片、视频请用 ReadImage / ReadVideo。需要读多个文件时优先并行:在同一条回复里发出多个 read 调用,"
                     "而不是每轮读一个。"),
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
        try:
            text = await asyncio.to_thread(_read_text, path)
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, content=None, error=str(exc), failure_kind="validation",
                              rendered=f"[Read failed: {exc}]")
        lines = text.splitlines()
        offset = int(invocation.input.get("offset") or 0)
        limit = int(invocation.input.get("limit") or _DEFAULT_LIMIT)
        chunk = lines[offset : offset + limit]
        rendered = "\n".join(f"{offset + i + 1:>5}\t{line}" for i, line in enumerate(chunk))
        return ToolResult(ok=True, content=text, rendered=rendered)


def _read_text(path: Path) -> str:
    limit = 8 * 1024 * 1024
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Read 只支持普通文本文件")
        head = file.read(8192)
        if detect_format(head):
            raise ValueError("这是图片或视频，请使用 ReadImage / ReadVideo 查看实际内容")
        if info.st_size > limit:
            raise ValueError("文本超过 8 MiB，请用 Bash 按范围提取后读取")
        data = head + file.read(limit + 1 - len(head))
    if len(data) > limit:
        raise ValueError("文本在读取时超过 8 MiB")
    if b"\0" in data:
        raise ValueError("Read 不支持二进制文件；图片或视频请使用 ReadImage / ReadVideo")
    return data.decode("utf-8", errors="replace")
