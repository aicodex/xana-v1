"""Feishu 渠道：归一化输入、配对校验、按 run 渲染回答卡及精确取消。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels.card import Card
from xana.agent_adapter.channels.feishu_events import parse_message_event
from xana.agent_adapter.channels.process_view import DEFAULT_IM_VIEW_MODE as DEFAULT_VIEW_MODE
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState,
    build_help_menu,
    build_notice_card,
)
from xana.agent_adapter.channels.unified_card_feishu import render as render_unified
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    MediaSendError,
    OutboundMedia,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import ProgressEvent
from xana.multimodal.files import FileMediaStore, MAX_ATTACHMENTS


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    api_base: str = "https://open.feishu.cn"
    bot_open_id: str | None = None


# ---------------------------------------------------------------------------
# FeishuApiSender Protocol (structural)
# ---------------------------------------------------------------------------


class FeishuApiSender:
    async def download_media(self, *, message_id: str, file_key: str, kind: str) -> bytes:
        raise NotImplementedError

    async def send_card(
        self, *, receive_id: str, card: dict,
        reply_message_id: str | None = None,
    ) -> str:  # pragma: no cover
        raise NotImplementedError

    async def patch_card(self, *, message_id: str, card: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    async def send_media(self, *, receive_id: str, media: OutboundMedia) -> str:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-session card registry
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    session_id: str = ""
    # card_id -> Card. card_id == run_id for answer cards; dedicated id for menus.
    cards: dict[str, Card] = field(default_factory=dict)
    # 待用于下一次新卡片首次 send 的 reply_to(飞书原话引用框)。
    pending_reply_to: str | None = None


# ---------------------------------------------------------------------------
# FeishuAdapter
# ---------------------------------------------------------------------------


class FeishuAdapter(Adapter):
    adapter_id = "feishu"

    def __init__(
        self,
        *,
        config: FeishuConfig,
        api_sender: FeishuApiSender | None = None,
        pairing: PairingStore | None = None,
        session_resolver: Any,
        media_store: FileMediaStore | None,
    ) -> None:
        self._config = config
        self._media_store = media_store
        self._api_sender = api_sender
        self._pairing = pairing
        self._resolver = session_resolver
        self._status: str = "starting"
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._buffers: dict[str, _SessionBuffer] = {}
        self._ws_client: Any | None = None
        self._pending_tasks: list[asyncio.Task] = []
        self._gateway: Any | None = None
        self._menu_seq: dict[str, int] = {}

    def set_gateway(self, gateway: Any) -> None:
        self._gateway = gateway

    def rotate_session(self, external_thread_id: str) -> str:
        return self._resolver.rotate(self.adapter_id, external_thread_id)

    async def send_media(self, *, chat_id: str, media: OutboundMedia) -> str:
        """发原生图片/视频消息。与卡片链路独立,不改 per-session 卡片状态。"""
        if self._api_sender is None:
            raise MediaSendError("飞书渠道尚未启动", failure_kind="unsupported")
        return await self._api_sender.send_media(receive_id=chat_id, media=media)

    def _allocate_sid(self, external_thread_id: str) -> str:
        return self._resolver.resolve(self.adapter_id, external_thread_id)

    async def _drain_pending(self) -> None:
        while self._pending_tasks:
            pending = self._pending_tasks
            self._pending_tasks = []
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._api_sender is None:
            from xana.agent_adapter.channels.feishu_api import build_real_sender
            self._api_sender = build_real_sender(self._config)
        from xana.agent_adapter.channels.feishu_ws import FeishuWSClient
        self._ws_client = FeishuWSClient(
            self._config.app_id,
            self._config.app_secret,
            domain=self._config.api_base,
            on_im_message=self._on_event,
            on_card_action=self._on_card_action,
        )
        await self._ws_client.start(asyncio.get_running_loop())
        self._status = "healthy"

    async def stop(self) -> None:
        if self._ws_client is not None:
            await self._ws_client.stop()
            self._ws_client = None
        self._status = "stopped"

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive_gen()

    async def _receive_gen(self) -> AsyncGenerator[InboundMessage, None]:
        while True:
            msg = await self._inbound.get()
            yield msg

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    async def _on_event(self, event: dict[str, Any]) -> None:
        parsed = parse_message_event(event)
        if parsed is None:
            return
        sender_id = parsed.sender_open_id or ""
        if not await self._gate_pairing(sender_id, parsed.chat_id, parsed.text):
            return
        attachments = []
        try:
            if len(parsed.resources) > MAX_ATTACHMENTS:
                raise ValueError(f"单条消息最多支持 {MAX_ATTACHMENTS} 个媒体附件")
            for resource in parsed.resources:
                if self._api_sender is None or self._media_store is None:
                    raise ValueError("媒体接收服务未配置")
                data = await self._api_sender.download_media(message_id=parsed.message_id,
                    file_key=resource.file_key, kind=resource.kind)
                media = await asyncio.to_thread(self._media_store.save_bytes, data)
                attachments.append({"file_path": media.path})
        except Exception as exc:
            if self._api_sender is None:
                raise
            card = render_unified(build_notice_card(card_id="media-error",
                text=f"媒体接收失败，请重新发送：{exc}"))
            await self._api_sender.send_card(receive_id=parsed.chat_id, card=card,
                reply_message_id=parsed.message_id or None)
            return
        session_id = self._allocate_sid(parsed.chat_id)
        buf = self._get_or_create_buffer(session_id)
        buf.chat_id = parsed.chat_id
        # 记下用户消息 id,下一张新卡 send 时用 reply_to 引用
        buf.pending_reply_to = parsed.message_id or None
        msg = InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=parsed.text,
            attachments=tuple(attachments),
            user_id=parsed.sender_open_id,
            metadata={"chat_id": parsed.chat_id, "message_id": parsed.message_id},
        )
        await self._inbound.put(msg)

    async def _gate_pairing(self, user_id: str, chat_id: str, text: str) -> bool:
        if self._pairing is None:
            return True
        if not user_id:
            return False
        if self._pairing.is_allowed("feishu", user_id):
            return True
        newly_pending = self._pairing.add_pending(
            "feishu", user_id, first_message=text, chat_id=chat_id
        )
        if newly_pending and self._api_sender is not None:
            hint = (
                "您尚未配对。请管理员运行: "
                f"`xana pair approve feishu {user_id}`"
            )
            card = render_unified(build_notice_card(card_id="pairing", text=hint))
            await self._api_sender.send_card(receive_id=chat_id, card=card)
        return False

    async def _on_card_action(self, event: dict[str, Any]) -> None:
        value = (event.get("action") or {}).get("value", {}) or {}
        chat_id = str((event.get("context") or {}).get("open_chat_id", "") or "")
        # 回调动作：send_text / load_tools / cancel。
        # 加载剩余工具:adapter 本地动作,不走 router,只刷卡。
        load_card = value.get("load_tools")
        if load_card:
            await self._handle_load_tools(chat_id, str(load_card))
            return
        if "steer" in value:
            open_id = str((event.get("operator") or {}).get("open_id", "") or "")
            if self._pairing is not None and not await self._gate_pairing(open_id, chat_id, ""):
                return
            if self._gateway is not None:
                for sid, buf in self._buffers.items():
                    if buf.chat_id == chat_id and any(card.state.pending_input_id == value["steer"] for card in buf.cards.values()):
                        try:
                            await self._gateway.control.promote(sid, str(value["steer"]))
                        except (LookupError, ValueError):
                            pass  # A consumed/stale button cannot steer another input.
                        return
            return
        if "cancel" in value or "compact_cancel" in value:
            action = "cancel" if "cancel" in value else "compact_cancel"
            open_id = str((event.get("operator") or {}).get("open_id", "") or "")
            if self._pairing is not None and not await self._gate_pairing(open_id, chat_id, ""):
                return
            rid = str(value[action])
            if self._gateway is not None:
                for sid, buf in self._buffers.items():
                    if buf.chat_id == chat_id and rid in buf.cards:
                        if action == "cancel":
                            await self._gateway.cancel(sid, rid)
                        else:
                            await self._inbound.put(InboundMessage(self.adapter_id, sid, f"/compact_cancel {rid}",
                                user_id=open_id, metadata={"chat_id": chat_id}))
                        return
            return
        send_text = str(value.get("send_text", "") or "")
        open_id = str((event.get("operator") or {}).get("open_id", "") or "")
        if not send_text or not chat_id:
            return
        if self._pairing is not None and not await self._gate_pairing(
            open_id, chat_id, send_text
        ):
            return
        session_id = self._allocate_sid(chat_id)
        self._get_or_create_buffer(session_id).chat_id = chat_id
        await self._inbound.put(
            InboundMessage(
                adapter_id=self.adapter_id,
                session_id=session_id,
                text=send_text,
                user_id=open_id or None,
                metadata={"chat_id": chat_id, "source": "card_action"},
            )
        )

    async def _handle_load_tools(self, chat_id: str, card_id: str) -> None:
        """加载剩余工具按钮回调:委托给 Card。"""
        if not chat_id:
            return
        session_id = self._allocate_sid(chat_id)
        buf = self._get_or_create_buffer(session_id)
        card = buf.cards.get(card_id)
        if card is None:
            await self._send_stale_notice(chat_id)
            return
        card.reveal_tools()

    async def _send_stale_notice(self, chat_id: str) -> None:
        """卡片不存在(/clear 后或重启后)→ 发个小卡告诉用户。
        飞书没原生 toast,只能发一条新消息。"""
        if self._api_sender is None:
            return
        notice = {
            "schema": "2.0",
            "config": {"width_mode": "compact"},
            "body": {"elements": [
                {"tag": "markdown",
                 "content": "_此卡片已过期(/clear 后旧会话不可恢复)_"},
            ]},
        }
        try:
            await self._api_sender.send_card(receive_id=chat_id, card=notice)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Session binding helpers
    # ------------------------------------------------------------------

    async def bind_session(self, binding: SessionBinding) -> None:
        self._get_or_create_buffer(binding.session_id).chat_id = binding.external_thread_id

    # ------------------------------------------------------------------
    # Render (AgentEvent path)
    # ------------------------------------------------------------------

    async def render(self, event: AgentEvent) -> RenderedMessage:
        buf = self._get_or_create_buffer(event.session_id)
        card_id = self._card_id_for(event)
        if not card_id:
            return RenderedMessage(
                adapter_id=self.adapter_id,
                session_id=event.session_id,
                text="",
                metadata={"event_kind": event.kind},
            )
        card = self._ensure_card(buf, card_id, run_id=card_id)
        task = card.dispatch(event)
        if task is not None:
            try:
                await task
            except Exception:
                pass
        text_out = (
            card.state.answer.answer_text if card.state.answer else ""
        )
        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=text_out,
            metadata={"event_kind": event.kind},
        )

    def _card_id_for(self, event: AgentEvent) -> str:
        if event.harness_event is not None and event.harness_event.run_id:
            return event.harness_event.run_id
        rid = (event.payload or {}).get("run_id")
        if rid:
            return str(rid)
        return ""

    def _make_card(self, buf: _SessionBuffer, state: UnifiedCardState) -> Card:
        """新 Card + 注入交付/任务追踪。"""
        card = Card(state)
        card.bind_transport(
            deliver=lambda c: self._deliver(buf, c),
            task_sink=lambda t: self._pending_tasks.append(t),
        )
        return card

    def _ensure_card(
        self, buf: _SessionBuffer, card_id: str, *, run_id: str | None = None
    ) -> Card:
        card = buf.cards.get(card_id)
        if card is None:
            # 工具过程默认按组折叠(IM 卡片放不下逐条调用);会话 /view 可覆盖
            state = UnifiedCardState(card_id=card_id, run_id=run_id, view_mode=DEFAULT_VIEW_MODE)
            card = self._make_card(buf, state)
            buf.cards[card_id] = card
        return card

    def _next_menu_card_id(self, session_id: str, *, prefix: str) -> str:
        n = self._menu_seq.get(session_id, 0) + 1
        self._menu_seq[session_id] = n
        return f"{prefix}-{session_id}-{n}"

    async def _deliver(self, buf: _SessionBuffer, card: Card) -> None:
        """Card 的 deliver 钩子:渲染 + send_card / patch_card。"""
        if buf.chat_id is None or self._api_sender is None:
            return
        schema = render_unified(card.state)
        if card.message_id is None:
            # 首次 send 用 reply_to(显示用户原话引用框);后续 patch 不需要。
            reply_to = buf.pending_reply_to
            buf.pending_reply_to = None
            mid = await self._api_sender.send_card(
                receive_id=buf.chat_id, card=schema,
                reply_message_id=reply_to,
            )
            card.message_id = mid
        else:
            await self._api_sender.patch_card(
                message_id=card.message_id, card=schema
            )

    # ------------------------------------------------------------------
    # ProgressEvent path (note_progress)
    # ------------------------------------------------------------------

    def note_progress(self, payload: dict) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        buf = self._get_or_create_buffer(sid)
        kind = payload.get("kind")
        # Menu cards (one-shot, no answer).
        if kind == "menu_response":
            specs = tuple((str(n), str(d)) for n, d in (payload.get("specs") or ()))
            cid = self._next_menu_card_id(sid, prefix="help")
            state = build_help_menu(card_id=cid, specs=specs)
            card = self._make_card(buf, state)
            card.terminal = True
            buf.cards[cid] = card
            self._spawn_task(self._deliver(buf, card))
            return
        rid = str(payload.get("run_id") or "")
        if not rid:
            return
        card = self._ensure_card(buf, rid, run_id=rid)
        pe = ProgressEvent(
            session_id=sid, run_id=rid,
            message=str(kind or ""), payload=dict(payload),
        )
        card.dispatch(pe)

    def _spawn_task(self, coro) -> None:
        try:
            t = asyncio.create_task(coro)
        except RuntimeError:
            return
        self._pending_tasks.append(t)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> AdapterHealth:
        status = self._status
        if (
            status == "healthy"
            and self._ws_client is not None
            and not self._ws_client.thread_alive()
        ):
            status = "degraded"
        return AdapterHealth(adapter_id=self.adapter_id, status=status)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_buffer(self, session_id: str) -> _SessionBuffer:
        if session_id not in self._buffers:
            self._buffers[session_id] = _SessionBuffer(session_id=session_id)
        return self._buffers[session_id]
