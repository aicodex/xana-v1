"""Adapter and gateway abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Any

from xana.agent_adapter.contracts import (
    AdapterHealth, CommandResult, CommandSpec, InboundMessage, MediaSendError, OutboundMedia,
    RenderedMessage, SessionBinding, TaskPanelRow,
)
from xana.agents.contracts import AgentEvent, AgentRequest


class CommandRouter(ABC):
    @abstractmethod
    async def list_commands(self) -> tuple[CommandSpec, ...]: ...

    @abstractmethod
    async def route_command(self, inbound: InboundMessage) -> CommandResult: ...


class AgentRequestRouter(ABC):
    @abstractmethod
    async def route(self, inbound: InboundMessage) -> AgentRequest: ...


class Adapter(ABC):
    @abstractmethod
    def note_progress(self, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def receive(self) -> AsyncIterator[InboundMessage]: ...

    @abstractmethod
    async def render(self, event: AgentEvent) -> RenderedMessage: ...

    @abstractmethod
    async def bind_session(self, binding: SessionBinding) -> None: ...

    @abstractmethod
    async def health(self) -> AdapterHealth: ...

    async def send_media(self, *, chat_id: str, media: OutboundMedia) -> str:
        """发送图片/视频原生消息,返回渠道消息 id。默认 = 该渠道不支持。"""
        raise MediaSendError(
            "当前渠道不支持发送图片/视频(目前仅飞书支持)", failure_kind="unsupported")


class AgentGateway(ABC):
    @abstractmethod
    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None: ...

    @abstractmethod
    async def register_command_router(self, router: CommandRouter) -> None: ...

    @abstractmethod
    def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]: ...

    @abstractmethod
    async def accept(self, inbound: InboundMessage) -> bool:
        """Return durable admission; service retains event rendering until execution ends."""
        ...

    @abstractmethod
    async def cancel(self, session_id: str, run_id: str | None = None) -> None: ...

    @abstractmethod
    async def list_inflight(self, session_id: str | None = None) -> tuple[TaskPanelRow, ...]: ...

    @abstractmethod
    async def resume(self, session_id: str, run_id: str) -> None: ...

    @abstractmethod
    async def cancel_all(self) -> None:
        """Close admission and stop all active executions for process shutdown."""
        ...

    @abstractmethod
    def has_active_runs(self) -> bool:
        """Whether this service still owns active execution (including maintenance)."""
        ...
