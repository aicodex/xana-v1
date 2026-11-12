"""ReadMedia: expose actual image/video inputs to the current model."""
from __future__ import annotations

import asyncio

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.multimodal.files import FileMediaStore
from xana.multimodal.contracts import InputModality
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool
from xana.tools.builtin._path import resolve_path

class ReadMediaTool(Tool):
    def __init__(self, store: FileMediaStore, kind: InputModality) -> None:
        self._store = store
        self._kind = kind
        name, label, formats = ("ReadImage", "图片", "PNG/JPEG/WEBP/GIF，≤10 MiB") if kind == "image" else ("ReadVideo", "视频", "MP4，≤100 MiB")
        self._spec = ToolSpec(
            component=ComponentSpec(kind="tool", name=name, qualified_name=f"builtin.{name}",
                source=ComponentSource(source_type="builtin", scope="system"),
                description=f"读取{label}，让当前模型实际查看画面。支持 {formats}。生成或修改后用此工具查看，不要仅凭文件名推测内容。"),
            input_schema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
            temporal="static", toolset="fs", input_modality=kind, read_only=True, concurrency="parallel",
            display=ToolDisplay(category="read", verb=f"查看{label}", summary_args=("file_path",)),
        )

    def spec(self) -> ToolSpec:
        return self._spec

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only media input")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            if self._kind not in context.model_input_modalities:
                raise ValueError(f"当前模型未开启 {self._kind} 输入")
            path = invocation.input.get("file_path")
            if not isinstance(path, str) or not path:
                raise ValueError("file_path 必须是非空文件路径")
            media = await asyncio.to_thread(self._store.import_file, resolve_path(path, context.cwd))
            if media.kind != self._kind:
                raise ValueError(f"文件类型为 {media.kind}，与 {self._spec.component.name} 不符")
            text = f"已载入 {media.kind}: {path} ({media.mime_type}, {media.size_bytes} bytes)"
            return ToolResult(ok=True, content=text, rendered=text, media=(media,))
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, content=None, error=str(exc), failure_kind="validation", rendered=f"[{self._spec.component.name} failed: {exc}]")
