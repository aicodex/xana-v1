"""DefaultAgentRequestRouter: InboundMessage -> AgentRequest.

v1 routes everything to a single default agent. Future versions can
parse `/agent_name` prefixes or look up by adapter/channel.
"""
from __future__ import annotations

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.interfaces import AgentRequestRouter
from xana.agents.contracts import AgentRequest


class DefaultAgentRequestRouter(AgentRequestRouter):
    def __init__(self, default_agent: str) -> None:
        self.default_agent = default_agent

    async def route(self, inbound: InboundMessage) -> AgentRequest:
        return AgentRequest(
            session_id=inbound.session_id,
            text=inbound.text,
            source=inbound.adapter_id,
            target_agent=self.default_agent,
            attachments=inbound.attachments,
            metadata=dict(inbound.metadata),
        )
