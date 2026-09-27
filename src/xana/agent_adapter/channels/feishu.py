"""FeishuAdapter: Feishu (Lark) channel adapter — unified PH card edition (T10).

Each PH (or menu card) owns one ``UnifiedCardState`` keyed by card_id.
Events flow through ``unified_card_events.apply_event(state, event)`` and the
result is rendered via ``unified_card_feishu.render(state)``; the adapter
sends or patches a single lark message per card. Throttle = DraftCoalescer
0.25s. Replaces old build_fg_streaming_card / build_bg_placeholder_card /
build_frozen_ph_card / build_attach_menu_card paths.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels.card import Card
from xana.agent_adapter.channels.feishu_events import parse_message_event
from xana.agent_adapter.channels.process_view import DEFAULT_IM_VIEW_MODE as DEFAULT_VIEW_MODE
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState,
    build_attach_menu,
    build_help_menu,
    build_notice_card,
)
from xana.agent_adapter.channels.unified_card_feishu import render as render_unified
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import ProgressEvent


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str | None = None
    api_base: str = "https://open.feishu.cn"
    bot_open_id: str | None = None
    connection_mode: str = "ws"  # "ws" | "webhook"


# ---------------------------------------------------------------------------
# FeishuApiSender Protocol (structural)
# ---------------------------------------------------------------------------


class FeishuApiSender:
    async def send_card(
        self, *, receive_id: str, card: dict,
        reply_message_id: str | None = None,
    ) -> str:  # pragma: no cover
        raise NotImplementedError

    async def patch_card(self, *, message_id: str, card: dict) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-session card registry
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    session_id: str = ""
    # card_id -> Card. card_id == run_id for PH cards; dedicated id for menus.
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
        bindings: BindingStore,
        api_sender: FeishuApiSender | None = None,
        pairing: PairingStore | None = None,
        session_resolver: Any = None,
    ) -> None:
        self._config = config
        self._bindings = bindings
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
        if self._resolver is not None:
            return self._resolver.rotate("feishu", external_thread_id)
        return self._bindings.rotate(external_thread_id, prefix="feishu")

    def _allocate_sid(self, external_thread_id: str) -> str:
        if self._resolver is not None:
            return self._resolver.resolve("feishu", external_thread_id)
        return self._bindings.allocate(external_thread_id, prefix="feishu")

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
            from xana.agent_adapter.channels.feishu_webhook import build_real_sender
            self._api_sender = build_real_sender(self._config)
        if self._config.connection_mode == "ws":
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
        session_id = self._allocate_sid(parsed.chat_id)
        buf = self._get_or_create_buffer(session_id)
        buf.chat_id = parsed.chat_id
        # 记下用户消息 id,下一张新卡 send 时用 reply_to 引用
        buf.pending_reply_to = parsed.message_id or None
        msg = InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=parsed.text,
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
        # 回调 value 只有三种(unified_card_feishu._btn):send_text / load_tools / detach。
        # 加载剩余工具:adapter 本地动作,不走 router,只刷卡。
        load_card = value.get("load_tools")
        if load_card:
            await self._handle_load_tools(chat_id, str(load_card))
            return
        if "detach" in value:  # 值可能为空串(无 ph_run_id),按键判断
            if not chat_id or self._gateway is None:
                return
            session_id = self._allocate_sid(chat_id)
            try:
                await self._gateway.detach_foreground(session_id)
            except Exception:
                pass
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

    async def bind_session_for_chat(self, chat_id: str, session_id: str) -> None:
        self._bindings.put(chat_id, session_id)
        self._get_or_create_buffer(session_id).chat_id = chat_id

    async def bind_session(self, binding: SessionBinding) -> None:
        chat_id = binding.external_thread_id
        session_id = binding.session_id
        self._bindings.put(chat_id, session_id)
        self._get_or_create_buffer(session_id).chat_id = chat_id

    # ------------------------------------------------------------------
    # Render (AgentEvent path)
    # ------------------------------------------------------------------

    async def render(self, event: AgentEvent) -> RenderedMessage:
        buf = self._get_or_create_buffer(event.session_id)
        # /attach menu-card detour: payload may carry attach_choices.
        choices = event.payload.get("attach_choices") if event.payload else None
        if isinstance(choices, list) and choices:
            cands = tuple(
                (str(c.get("run_id") or ""), str(c.get("label") or c.get("run_id") or ""))
                for c in choices
                if c.get("run_id")
            )
            card_id = self._next_menu_card_id(event.session_id, prefix="attach")
            state = build_attach_menu(card_id=card_id, candidates=cands)
            card = self._make_card(buf, state)
            card.terminal = True   # 菜单卡一次性
            buf.cards[card_id] = card
            await self._direct_send_or_patch(buf, card)
            return RenderedMessage(
                adapter_id=self.adapter_id,
                session_id=event.session_id,
                text=str(event.payload.get("text", "") or ""),
                metadata={"event_kind": event.kind, "attach_menu": True},
            )

        card_id = self._card_id_for(event)
        if not card_id:
            return RenderedMessage(
                adapter_id=self.adapter_id,
                session_id=event.session_id,
                text="",
                metadata={"event_kind": event.kind},
            )
        card = self._ensure_card(buf, card_id, ph_run_id=card_id)
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
        self, buf: _SessionBuffer, card_id: str, *, ph_run_id: str | None = None
    ) -> Card:
        card = buf.cards.get(card_id)
        if card is None:
            # 工具过程默认按组折叠(IM 卡片放不下逐条调用);会话 /view 可覆盖
            state = UnifiedCardState(card_id=card_id, ph_run_id=ph_run_id, view_mode=DEFAULT_VIEW_MODE)
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

    async def _direct_send_or_patch(self, buf: _SessionBuffer, card: Card) -> None:
        """绕开 coalescer 直接交付(用于 attach_choices 这种命令直送)。"""
        await self._deliver(buf, card)

    # ------------------------------------------------------------------
    # ProgressEvent path (note_progress)
    # ------------------------------------------------------------------

    def note_progress(self, payload: dict) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        buf = self._get_or_create_buffer(sid)
        kind = payload.get("kind")
        # Menu cards (one-shot, no PH).
        if kind in ("menu_response", "attach_options"):
            if kind == "menu_response":
                specs = tuple(
                    (str(n), str(d)) for n, d in (payload.get("specs") or ())
                )
                cid = self._next_menu_card_id(sid, prefix="help")
                state = build_help_menu(card_id=cid, specs=specs)
            else:
                cands = tuple(
                    (str(r), str(l)) for r, l in (payload.get("candidates") or ())
                )
                cid = self._next_menu_card_id(sid, prefix="attach")
                state = build_attach_menu(card_id=cid, candidates=cands)
            card = self._make_card(buf, state)
            card.terminal = True
            buf.cards[cid] = card
            self._spawn_task(self._deliver(buf, card))
            return
        rid = str(payload.get("run_id") or "")
        if not rid:
            return
        card = self._ensure_card(buf, rid, ph_run_id=rid)
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
