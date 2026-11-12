"""send_media:把 workspace 内的图片/视频发到当前聊天(支持飞书和 Telegram)。

只接受图片与 MP4 视频,不接受任意文件;发送目标固定为当前会话所属聊天,模型不能指定接收方。
agent 用它在生成图片/视频后立刻给用户看,用户确认后再继续下一步。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from xana.agent_adapter.contracts import MediaSendError, OutboundMedia
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._media import (
    MAX_IMAGE_MB,
    MAX_VIDEO_MB,
    MediaFileError,
    extract_video_cover_jpeg,
    probe_video_duration_ms,
    read_workspace_media,
)
from xana.tools.contracts import (
    ToolContext, ToolDisplay, ToolFailureKind, ToolInvocation, ToolResult, ToolSpec,
)
from xana.tools.interfaces import Tool

if TYPE_CHECKING:
    from xana.runtime.delivery import MediaDelivery


_DESCRIPTION = (
    "把当前工作区内的图片或视频发到当前聊天(支持飞书和 Telegram),用户会收到一条可直接查看/播放的独立消息。"
    "生成或处理完图片/视频后用它给用户看,不要用文本发本地路径。"
    f"图片支持 PNG/JPEG/WEBP/GIF(≤{MAX_IMAGE_MB}MB);视频只支持 MP4(≤{MAX_VIDEO_MB}MB),"
    "会自动读取时长并抽首帧作封面,也可以用 cover 指定封面图。"
    "只接受图片和视频,不接受其他文件;路径必须在当前工作区内(符号链接解析后也不能逃出工作区)。"
)

_SPEC = ToolSpec(
    temporal="static",
    destructive=True,
    toolset="media",
    component=ComponentSpec(
        kind="tool",
        name="send_media",
        qualified_name="builtin.send_media",
        description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "图片或 MP4 视频路径;相对路径按当前工作区解析,解析后必须仍在工作区内。",
            },
            "cover": {
                "type": "string",
                "description": "可选:视频封面图片路径(仅视频);不填则自动抽首帧。",
            },
        },
        "required": ["file_path"],
    },
    open_world=True,
    display=ToolDisplay(category="other", verb="发送图片/视频", unit="个", summary_args=("file_path",),
                        result="none"),
)


class SendMediaTool(Tool):
    def __init__(self, *, delivery: "MediaDelivery") -> None:
        self._delivery = delivery

    def spec(self) -> ToolSpec:
        return _SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="把媒体发到当前会话所属聊天")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        raw_path = invocation.input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail("validation", "未发送:file_path 不能为空")

        try:
            source = read_workspace_media(raw_path, context.cwd)
        except MediaFileError as exc:
            return _fail(exc.failure_kind, str(exc))
        except OSError as exc:
            return _fail("execution", f"未发送:读取文件失败:{exc}")

        cover: bytes | None = None
        cover_filename: str | None = None
        cover_content_type: str | None = None
        duration_ms: int | None = None

        raw_cover = invocation.input.get("cover")
        if source.kind == "image":
            if isinstance(raw_cover, str) and raw_cover.strip():
                return _fail("validation", "未发送:cover 只用于视频")
        else:
            duration_ms = await probe_video_duration_ms(source.path)
            if isinstance(raw_cover, str) and raw_cover.strip():
                try:
                    cover_file = read_workspace_media(raw_cover, context.cwd)
                except MediaFileError as exc:
                    return _fail(exc.failure_kind, f"封面未发送:{exc}")
                except OSError as exc:
                    return _fail("execution", f"封面未发送:读取文件失败:{exc}")
                if cover_file.kind != "image":
                    return _fail("validation", "未发送:cover 必须是图片")
                cover = cover_file.data
                cover_filename = cover_file.filename
                cover_content_type = cover_file.content_type
            else:
                cover = await extract_video_cover_jpeg(source.path)
                if cover is not None:
                    cover_filename = f"{Path(source.filename).stem}-cover.jpg"
                    cover_content_type = "image/jpeg"

        media = OutboundMedia(
            kind=source.kind,
            data=source.data,
            filename=source.filename,
            content_type=source.content_type,
            cover=cover,
            cover_filename=cover_filename,
            cover_content_type=cover_content_type,
            duration_ms=duration_ms,
        )
        try:
            message_id = await self._delivery.send(session_id=context.session_id, media=media)
        except MediaSendError as exc:
            return _fail(exc.failure_kind, f"未发送:{exc}", retryable=exc.retryable)
        except Exception as exc:  # noqa: BLE001 - 工具失败要回给模型,不能打断回合
            return _fail("execution", f"未发送:{exc}")

        label = "图片" if source.kind == "image" else "视频"
        suffix = f"(时长 {duration_ms / 1000:.1f} 秒)" if source.kind == "video" and duration_ms else ""
        return ToolResult(
            ok=True,
            content=None,
            rendered=f"已发送{label}到当前聊天:{source.filename}{suffix}",
            metadata={"message_id": message_id, "display_text": f"已发送{label}"},
        )


def _fail(failure_kind: ToolFailureKind, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False,
        content=None,
        error=message,
        failure_kind=failure_kind,
        rendered=f"[send_media] {message}",
        retryable=retryable,
    )
