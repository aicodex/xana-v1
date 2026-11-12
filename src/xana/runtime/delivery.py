"""按会话送达:SessionDelivery 投递文本触发新的 agent turn;MediaDelivery 直接把图片/视频发到该会话的聊天。

SessionDelivery 的投递 = 经 gateway.submit 送一条入站消息;渠道按会话的路由选(没有路由按会话反查所属聊天,
再退回会话 id 前缀)。子会话(agent_run / 定时任务派生的会话)没有人接收结果,不投递。
MediaDelivery 只信 SessionResolver 的权威映射;子会话沿 parent_session_id 上溯;找不到就明确失败,不猜渠道。
"""
from __future__ import annotations

import logging
from typing import Any

from xana.agent_adapter.contracts import InboundMessage, MediaSendError, OutboundMedia, SessionBinding
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
                      source: str = "scheduler", input_id: str | None = None) -> bool:
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
        if input_id is not None:
            metadata["input_id"] = input_id
        if route and route[0] == adapter_id:
            # serve 重启后渠道还不知道这个会话对应哪个聊天:先绑定,回复卡片才能发出去
            await adapters[adapter_id].bind_session(SessionBinding(
                adapter_id=adapter_id, external_thread_id=route[1], session_id=session_id))
            metadata["chat_id"] = route[1]
        return await self._gateway.accept(InboundMessage(
            adapter_id=adapter_id, session_id=session_id, text=text, metadata=metadata))


class MediaDelivery:
    """把图片/视频发到会话所属聊天。

    路由只信 SessionResolver;子会话(agent_run / 定时任务)沿 SessionStore.parent_session_id 上溯。
    找不到或渠道未注册时明确失败——媒体绝不能像文本通知那样退回"第一个 adapter",否则会发错聊天。
    """

    _MAX_PARENT_DEPTH = 8

    def __init__(self, *, gateway: DefaultAgentGateway, resolver: SessionResolver | None = None,
                 session_store: SessionStore | None = None) -> None:
        self._gateway = gateway
        self._resolver = resolver
        self._sessions = session_store

    async def send(self, *, session_id: str, media: OutboundMedia) -> str:
        route = await self._resolve_route(session_id)
        if route is None:
            raise MediaSendError(
                "无法定位当前会话对应的聊天(会话可能已 /clear,或不是聊天渠道)",
                failure_kind="not_found",
            )
        adapter_id, chat_id = route
        adapter = self._gateway.adapters.get(adapter_id)
        if adapter is None:
            raise MediaSendError(
                f"渠道 {adapter_id} 尚未启动,无法发送图片/视频", failure_kind="unsupported")
        try:
            return await adapter.send_media(chat_id=chat_id, media=media)
        except MediaSendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一变成工具可见的失败
            _logger.warning("媒体发送失败(%s / %s): %s", adapter_id, session_id, exc)
            raise MediaSendError(f"发送失败:{exc}", failure_kind="execution") from exc

    async def _resolve_route(self, session_id: str) -> tuple[str, str] | None:
        if self._resolver is not None:
            route = self._resolver.route_of(session_id)
            if route is not None:
                return route
        if self._sessions is None:
            return None
        current = session_id
        seen = {session_id}
        for _ in range(self._MAX_PARENT_DEPTH):
            record = await self._sessions.get(current)
            if record is None or not record.parent_session_id:
                return None
            current = record.parent_session_id
            if current in seen:
                return None
            seen.add(current)
            if self._resolver is not None:
                route = self._resolver.route_of(current)
                if route is not None:
                    return route
        return None
