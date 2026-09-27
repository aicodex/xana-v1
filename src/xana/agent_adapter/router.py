"""DefaultAgentRequestRouter: InboundMessage -> AgentRequest.

会话绑定了 agent(SessionConfig.agent,由 /agent 设置)就路由给它,否则给默认 agent。
绑定的 agent 已不存在时由 gateway 报 unknown agent,不静默回退。
"""
from __future__ import annotations

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.interfaces import AgentRequestRouter
from xana.agents.contracts import AgentRequest
from xana.session_config.interfaces import SessionConfigStore


class DefaultAgentRequestRouter(AgentRequestRouter):
    def __init__(self, default_agent: str, *, session_config_store: SessionConfigStore | None = None) -> None:
        """session_config_store=None:不支持会话绑定,全部走默认 agent。"""
        self.default_agent = default_agent
        self._session_config_store = session_config_store

    async def route(self, inbound: InboundMessage) -> AgentRequest:
        target = self.default_agent
        if self._session_config_store is not None:
            bound = (await self._session_config_store.get(inbound.session_id)).agent
            target = bound or self.default_agent
        return AgentRequest(
            session_id=inbound.session_id,
            text=inbound.text,
            source=inbound.adapter_id,
            target_agent=target,
            attachments=inbound.attachments,
            metadata=dict(inbound.metadata),
        )
