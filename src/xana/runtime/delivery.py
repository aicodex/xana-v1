"""SessionDelivery:以系统身份往一个会话投递一条消息,触发该会话的 agent 处理(定时任务结果、后台任务完成通知)。

投递 = 经 gateway.submit 送一条入站消息;渠道按会话的路由选(没有路由按会话反查所属聊天,再退回会话 id 前缀)。
子会话(agent_run / 定时任务派生的会话)没有人接收结果,不投递。
"""
from __future__ import annotations

import logging
from typing import Any

from xana.agent_adapter.contracts import InboundMessage, SessionBinding
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.runtime.session_resolver import SessionResolver
from xana.state.interfaces import SessionStore

_logger = logging.getLogger(__name__)


class SessionDelivery:
    def __init__(self, *, gateway: DefaultAgentGateway, resolver: SessionResolver | None = None,
                 session_store: SessionStore | None = None) -> None:
        self._gateway = gateway
        self._resolver = resolver
        self._sessions = session_store

    async def deliver(self, session_id: str, text: str, *, route: tuple[str, str] | None = None,
                      source: str = "scheduler") -> bool:
        """投递成功返回 True;子会话或没有可用渠道返回 False(记日志)。"""
        if self._sessions is not None:
            rec = await self._sessions.get(session_id)
            if rec is not None and rec.parent_session_id:
                _logger.warning("会话 %s 是子会话,没有人接收结果,不投递:%s", session_id, text[:80])
                return False
        adapters = self._gateway.adapters
        if route is None and self._resolver is not None:
            route = self._resolver.route_of(session_id)      # 没记路由:按会话反查它属于哪个聊天
        adapter_id = route[0] if route else session_id.split("-", 1)[0]
        if adapter_id not in adapters:
            adapter_id = next(iter(adapters), None)
        if adapter_id is None:
            _logger.warning("没有可用的渠道,消息无法送达会话 %s", session_id)
            return False
        metadata: dict[str, Any] = {"source": source}
        if route and route[0] == adapter_id:
            # serve 重启后渠道还不知道这个会话对应哪个聊天:先绑定,回复卡片才能发出去
            await adapters[adapter_id].bind_session(SessionBinding(
                adapter_id=adapter_id, external_thread_id=route[1], session_id=session_id))
            metadata["chat_id"] = route[1]
        async for _rendered in self._gateway.submit(InboundMessage(
                adapter_id=adapter_id, session_id=session_id, text=text, metadata=metadata)):
            pass
        return True
