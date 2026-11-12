"""Agent adapter contracts.

Adapters translate external protocols into agent requests and render agent events back.
"""
from __future__ import annotations

import asyncio as _asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

AdapterHealthStatus = Literal["starting", "healthy", "degraded", "stopped", "failed"]
CommandVisibility = Literal["public", "admin", "debug"]


@dataclass(frozen=True)
class InboundMessage:
    adapter_id: str
    session_id: str
    text: str
    user_id: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


MediaKind = Literal["image", "video"]
MediaSendFailureKind = Literal["unsupported", "not_found", "execution"]


@dataclass(frozen=True)
class OutboundMedia:
    """渠道出站媒体。只表达图片/视频,从类型上排除任意文件发送。"""
    kind: MediaKind
    data: bytes = field(repr=False)
    filename: str
    content_type: str
    cover: bytes | None = field(default=None, repr=False)
    cover_filename: str | None = None
    cover_content_type: str | None = None
    duration_ms: int | None = None


class MediaSendError(RuntimeError):
    """渠道发送媒体失败。failure_kind 供工具层映射成 ToolResult.failure_kind。"""

    def __init__(
        self, message: str, *,
        failure_kind: MediaSendFailureKind = "execution",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.retryable = retryable


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    args_schema: dict[str, Any] = field(default_factory=dict)
    visibility: CommandVisibility = "public"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DraftHandle:
    adapter_id: str
    session_id: str
    draft_id: str


@dataclass(frozen=True)
class RenderedMessage:
    adapter_id: str
    session_id: str
    text: str
    draft: DraftHandle | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionBinding:
    adapter_id: str
    external_thread_id: str
    session_id: str
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterHealth:
    adapter_id: str
    status: AdapterHealthStatus
    details: dict[str, Any] = field(default_factory=dict)




@dataclass(frozen=True)
class TaskPanelRow:
    """任务面板一行：TurnCoordinator 活动状态与 RunStore 持久状态的合并视图。

    live=True 表示该 run 当前由 TurnCoordinator 执行；live=False
    表示只来自持久化 RunStore（中断或已完成）。
    """
    run_id: str
    session_id: str
    label: str
    status: RunStatus
    duration_s: float | None
    live: bool


# Re-exported for adapter-side typing convenience (状态字面量单一来源)。
from xana.state.contracts import RunStatus  # noqa: E402
