"""DefaultAgentGateway: wires Adapter <-> Agent."""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncGenerator, AsyncIterator

from xana.agent_adapter.contracts import (
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import (
    Adapter,
    AgentGateway,
    AgentRequestRouter,
    CommandRouter,
)
from xana.agents.contracts import AgentEvent
from xana.agents.interfaces import Agent


class DefaultAgentGateway(AgentGateway):
    def __init__(
        self,
        agents: dict[str, Agent],
        router: AgentRequestRouter,
    ) -> None:
        self.agents = agents
        self.router = router
        self.adapters: dict[str, Adapter] = {}
        self.command_router: CommandRouter | None = None
        self.bindings: dict[str, SessionBinding] = {}

    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None:
        self.adapters[adapter_id] = adapter

    async def register_command_router(self, router: CommandRouter) -> None:
        self.command_router = router

    async def attach(self, binding: SessionBinding) -> None:
        self.bindings[binding.session_id] = binding

    def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        return self._submit(inbound)

    async def _submit(self, inbound: InboundMessage) -> AsyncGenerator[RenderedMessage, None]:
        request = await self.router.route(inbound)
        target = request.target_agent or ""
        agent = self.agents.get(target)
        adapter = self.adapters.get(inbound.adapter_id)
        if agent is None or adapter is None:
            yield RenderedMessage(
                adapter_id=inbound.adapter_id,
                session_id=inbound.session_id,
                text=f"[gateway] unknown agent={target!r} or adapter={inbound.adapter_id!r}",
                metadata={"event_kind": "error"},
            )
            return
        async for event in agent.send(request):
            rendered = await adapter.render(event)
            yield self._attach_event_kind(rendered, event)

    def subscribe(self, adapter_id: str, session_id: str) -> AsyncIterator[RenderedMessage]:
        raise NotImplementedError("subscribe is not used by the CLI flow in v1")

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        for agent in self.agents.values():
            await agent.cancel(session_id, run_id)

    @staticmethod
    def _attach_event_kind(rendered: RenderedMessage, event: AgentEvent) -> RenderedMessage:
        metadata = dict(rendered.metadata)
        metadata.setdefault("event_kind", event.kind)
        return replace(rendered, metadata=metadata)
