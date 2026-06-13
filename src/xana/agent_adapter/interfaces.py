"""Adapter and gateway abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

from xana.agent_adapter.contracts import AdapterHealth, CommandResult, CommandSpec, DraftHandle, InboundMessage, RenderedMessage, SessionBinding
from xana.agents.contracts import AgentEvent, AgentRequest

if TYPE_CHECKING:
    from xana.agent_adapter.channels._draft import TurnDraft


class InboundParser(ABC):
    @abstractmethod
    async def parse(self, raw: object) -> InboundMessage: ...


class CommandRouter(ABC):
    @abstractmethod
    async def list_commands(self) -> tuple[CommandSpec, ...]: ...

    @abstractmethod
    async def route_command(self, inbound: InboundMessage) -> CommandResult: ...


class AgentRequestRouter(ABC):
    @abstractmethod
    async def route(self, inbound: InboundMessage) -> AgentRequest: ...


class DraftRenderer(ABC):
    @abstractmethod
    async def open(self, session_id: str) -> DraftHandle: ...

    @abstractmethod
    async def update(self, draft: DraftHandle, event: AgentEvent) -> RenderedMessage: ...

    @abstractmethod
    async def close(self, draft: DraftHandle, event: AgentEvent) -> RenderedMessage: ...


class Adapter(ABC):
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


class AgentGateway(ABC):
    @abstractmethod
    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None: ...

    @abstractmethod
    async def register_command_router(self, router: CommandRouter) -> None: ...

    @abstractmethod
    def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]: ...

    @abstractmethod
    async def attach(self, binding: SessionBinding) -> None: ...

    @abstractmethod
    def subscribe(self, adapter_id: str, session_id: str) -> AsyncIterator[RenderedMessage]: ...

    @abstractmethod
    async def cancel(self, session_id: str, run_id: str | None = None) -> None: ...


class CliRenderer(ABC):
    """Receives lifecycle callbacks for runs on a session and renders them to a CLI surface.

    Implementations bind to a session and react to per-run events emitted by the gateway:
    run lifecycle (started/detached/foreground_changed/completed_bg), incremental harness
    events, PH replacement (finalize to full card), and compact lifecycle.
    """

    @abstractmethod
    async def on_run_started(self, run_id: str, label: str) -> None: ...

    @abstractmethod
    async def on_event(self, run_id: str, event: AgentEvent) -> None: ...

    @abstractmethod
    async def on_run_detached(self, run_id: str) -> None: ...

    @abstractmethod
    async def on_foreground_changed(self, run_id: str) -> None: ...

    @abstractmethod
    async def on_ph_replaced(self, run_id: str, draft: "TurnDraft | None") -> None: ...

    @abstractmethod
    async def on_run_completed_bg(self, run_id: str, draft: "TurnDraft | None") -> None: ...

    @abstractmethod
    async def on_compact_started(self, run_id: str, fork_point: int) -> None: ...

    @abstractmethod
    async def on_compact_finalized(self, run_id: str, summary: str) -> None: ...
