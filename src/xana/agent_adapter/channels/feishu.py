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
from xana.agent_adapter.channels._draft import TurnDraft
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.feishu_card import build_card
from xana.agent_adapter.channels.feishu_events import parse_message_event
from xana.agent_adapter.channels.unified_card import (
    AnswerSection,
    UnifiedCardState,
    build_attach_menu,
    build_help_menu,
)
from xana.agent_adapter.channels.unified_card_events import apply_event
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
    async def send_card(self, *, receive_id: str, card: dict) -> str:  # pragma: no cover
        raise NotImplementedError

    async def patch_card(self, *, message_id: str, card: dict) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-card state holder
# ---------------------------------------------------------------------------


@dataclass
class _CardSlot:
    state: UnifiedCardState
    coalescer: DraftCoalescer = field(
        default_factory=lambda: DraftCoalescer(min_interval_s=0.25)
    )
    terminal: bool = False


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    session_id: str = ""
    # card_id -> slot. card_id == run_id for PH cards; dedicated id for menus.
    cards: dict[str, _CardSlot] = field(default_factory=dict)


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
    ) -> None:
        self._config = config
        self._bindings = bindings
        self._api_sender = api_sender
        self._pairing = pairing
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
        return self._bindings.rotate(external_thread_id, prefix="feishu")

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
        session_id = self._bindings.allocate(parsed.chat_id, prefix="feishu")
        self._get_or_create_buffer(session_id).chat_id = parsed.chat_id
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
            card = build_card(TurnDraft(run_id="?", content=hint), finalize=True)
            await self._api_sender.send_card(receive_id=chat_id, card=card)
        return False

    async def _on_card_action(self, event: dict[str, Any]) -> None:
        value = (event.get("action") or {}).get("value", {}) or {}
        chat_id = str((event.get("context") or {}).get("open_chat_id", "") or "")
        action = str(value.get("action", "") or "")
        send_field = str(value.get("send", "") or "")
        if action in ("switch_foreground", "attach_run"):
            target_run_id = str(value.get("run_id", "") or "")
            if not target_run_id or not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
            try:
                await self._gateway.attach(session_id, target_run_id)
            except Exception:
                pass
            return
        # Unified card top buttons emit value={"send": "/cancel <run_id>"} or
        # value={} for the "back to background" button. Adapter-local detach.
        if send_field.startswith("/cancel "):
            if not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
            rid = send_field.split(" ", 1)[1].strip()
            if rid:
                try:
                    await self._gateway.cancel(session_id, rid)
                except Exception:
                    pass
            return
        # Legacy/back-compat action names from earlier card builders.
        if action == "stop_foreground":
            if not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
            runs = getattr(self._gateway, "_inflight", {}).get(session_id, [])
            fg = next((ir for ir in runs if getattr(ir, "is_attached", False)), None)
            if fg is None:
                return
            try:
                await self._gateway.cancel(session_id, fg.run_id)
            except Exception:
                pass
            return
        if action == "detach_foreground" or value.get("detach"):
            if not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
            try:
                await self._gateway.detach_foreground(session_id)
            except Exception:
                pass
            return
        send_text = str(value.get("send_text", "") or send_field or "")
        open_id = str((event.get("operator") or {}).get("open_id", "") or "")
        if not send_text or not chat_id:
            return
        if self._pairing is not None and not await self._gate_pairing(
            open_id, chat_id, send_text
        ):
            return
        session_id = self._bindings.allocate(chat_id, prefix="feishu")
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
            cards = tuple(
                (str(c.get("run_id") or ""), str(c.get("label") or c.get("run_id") or ""))
                for c in choices
                if c.get("run_id")
            )
            card_id = self._next_menu_card_id(event.session_id, prefix="attach")
            state = build_attach_menu(card_id=card_id, candidates=cards)
            slot = _CardSlot(state=state)
            buf.cards[card_id] = slot
            await self._send_or_patch(buf, slot, finalize=True)
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
        slot = self._ensure_slot(buf, card_id, ph_run_id=card_id)
        if slot.terminal:
            # Card already terminal — drop late events to avoid resurrecting.
            return RenderedMessage(
                adapter_id=self.adapter_id,
                session_id=event.session_id,
                text=slot.state.answer.answer_text if slot.state.answer else "",
                metadata={"event_kind": event.kind, "drop": "terminal"},
            )
        apply_event(slot.state, event)
        # Determine finalize: harness final/error/cancelled → flush + mark terminal.
        hkind = (
            event.harness_event.kind
            if event.harness_event is not None
            else event.kind
        )
        finalize = hkind in ("final", "error", "cancelled")
        if finalize:
            slot.terminal = True
            slot.state.top_buttons = ()
            await slot.coalescer.finalize(self._make_send(buf, slot))
        else:
            slot.coalescer.flush(self._make_send(buf, slot))
        text_out = slot.state.answer.answer_text if slot.state.answer else ""
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

    def _ensure_slot(
        self, buf: _SessionBuffer, card_id: str, *, ph_run_id: str | None = None
    ) -> _CardSlot:
        slot = buf.cards.get(card_id)
        if slot is None:
            state = UnifiedCardState(card_id=card_id, ph_run_id=ph_run_id)
            slot = _CardSlot(state=state)
            buf.cards[card_id] = slot
        return slot

    def _next_menu_card_id(self, session_id: str, *, prefix: str) -> str:
        n = self._menu_seq.get(session_id, 0) + 1
        self._menu_seq[session_id] = n
        return f"{prefix}-{session_id}-{n}"

    def _make_send(self, buf: _SessionBuffer, slot: _CardSlot):
        async def _send() -> None:
            await self._send_or_patch(buf, slot, finalize=False)
        return _send

    async def _send_or_patch(
        self, buf: _SessionBuffer, slot: _CardSlot, *, finalize: bool
    ) -> None:
        if buf.chat_id is None or self._api_sender is None:
            return
        # Ensure top_buttons reflect current state.
        from xana.agent_adapter.channels.unified_card import compute_top_buttons
        if not slot.terminal and not finalize:
            slot.state.top_buttons = compute_top_buttons(slot.state)
        else:
            slot.state.top_buttons = ()
        card = render_unified(slot.state)
        if slot.state.message_id is None:
            mid = await self._api_sender.send_card(
                receive_id=buf.chat_id, card=card
            )
            slot.state.message_id = mid
        else:
            await self._api_sender.patch_card(
                message_id=slot.state.message_id, card=card
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
        # Menu responses get their own card.
        if kind == "menu_response":
            specs_raw = payload.get("specs") or ()
            specs = tuple((str(n), str(d)) for n, d in specs_raw)
            cid = self._next_menu_card_id(sid, prefix="help")
            state = build_help_menu(card_id=cid, specs=specs)
            slot = _CardSlot(state=state, terminal=True)
            buf.cards[cid] = slot
            self._schedule_send(buf, slot, finalize=True)
            return
        if kind == "attach_options":
            cands_raw = payload.get("candidates") or ()
            cands = tuple((str(rid), str(lbl)) for rid, lbl in cands_raw)
            cid = self._next_menu_card_id(sid, prefix="attach")
            state = build_attach_menu(card_id=cid, candidates=cands)
            slot = _CardSlot(state=state, terminal=True)
            buf.cards[cid] = slot
            self._schedule_send(buf, slot, finalize=True)
            return
        rid = str(payload.get("run_id") or "")
        if not rid:
            return
        slot = self._ensure_slot(buf, rid, ph_run_id=rid)
        if slot.terminal:
            return
        # Build a ProgressEvent and dispatch through apply_event.
        pe = ProgressEvent(
            session_id=sid,
            run_id=rid,
            message=str(kind or ""),
            payload=dict(payload),
        )
        apply_event(slot.state, pe)
        # Terminal kinds finalize the slot.
        finalize = kind in ("ph_replaced", "run_completed_bg")
        if finalize:
            slot.terminal = True
        # ph_replaced / detach call _schedule_send finalize; others go through
        # the throttle so high-rate compact_progress / usage_progress don't
        # spam the API.
        if finalize or kind == "run_detached":
            self._schedule_send(buf, slot, finalize=True)
        else:
            slot.coalescer.flush(self._make_send(buf, slot))

    def _schedule_send(
        self, buf: _SessionBuffer, slot: _CardSlot, *, finalize: bool
    ) -> None:
        async def _runner() -> None:
            if finalize:
                await slot.coalescer.finalize(
                    lambda: self._send_or_patch(buf, slot, finalize=True)
                )
            else:
                await self._send_or_patch(buf, slot, finalize=False)
        try:
            task = asyncio.create_task(_runner())
        except RuntimeError:
            return
        self._pending_tasks.append(task)

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
