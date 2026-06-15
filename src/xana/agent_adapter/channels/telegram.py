"""TelegramAdapter: Telegram channel adapter via Bot API long-polling.

Unified PH card edition (T11). Mirrors the feishu adapter structure:
each PH (or menu card) owns one ``UnifiedCardState`` keyed by card_id.
Events flow through ``unified_card_events.apply_event(state, event)`` and the
result is rendered via ``unified_card_telegram.render(state)``; the adapter
sends (first time) or edits (subsequent) a single Telegram message per card.
Throttle = DraftCoalescer 0.25s.

Replaces the prior per-turn streaming + dual-card model (build_fg / bg
placeholder / frozen PH renderers). Callback data:

- Top "停止" button -> ``/cancel <run_id>`` (synthesized inbound message).
- Top "后台" button -> synthetic ``__detach__:<run_id>`` -> gateway.detach_foreground.
- Bottom attach menu -> ``/attach <run_id>``.
- Bottom help-menu commands -> the command string (e.g. ``/clear``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState,
    build_attach_menu,
    build_help_menu,
    compute_top_buttons,
)
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agent_adapter.channels.unified_card_telegram import render as render_unified
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import ProgressEvent

_logger = logging.getLogger(__name__)

# Backoff after a getUpdates failure. Module constant so tests can zero it.
_POLL_BACKOFF_S = 3.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    api_base: str = "https://api.telegram.org"
    poll_timeout_s: int = 25
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# TelegramApiClient Protocol (structural)
# ---------------------------------------------------------------------------


class TelegramApiClient:
    """Structural protocol — any object with these async methods qualifies."""

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> int:  # pragma: no cover
        raise NotImplementedError

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_updates(
        self, *, offset: int | None, timeout: int
    ) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    async def set_my_commands(self, commands: list[dict]) -> bool:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-card slot
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
# TelegramAdapter
# ---------------------------------------------------------------------------


class TelegramAdapter(Adapter):
    adapter_id = "telegram"

    def __init__(
        self,
        *,
        config: TelegramConfig,
        bindings: BindingStore,
        api_client: TelegramApiClient | None = None,
        pairing: PairingStore | None = None,
    ) -> None:
        self._config = config
        self._bindings = bindings
        self._api_client = api_client
        self._pairing = pairing
        self._status: str = "starting"
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._buffers: dict[str, _SessionBuffer] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._pending_tasks: list[asyncio.Task] = []
        self._gateway: Any | None = None
        self._menu_seq: dict[str, int] = {}

    def set_gateway(self, gateway: Any) -> None:
        self._gateway = gateway

    def rotate_session(self, external_thread_id: str) -> str:
        return self._bindings.rotate(external_thread_id, prefix="tg")

    async def _drain_pending(self) -> None:
        while self._pending_tasks:
            pending = self._pending_tasks
            self._pending_tasks = []
            await asyncio.gather(*pending, return_exceptions=True)

    def _schedule(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._pending_tasks.append(task)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._api_client is None:
            from xana.agent_adapter.channels.telegram_client import HttpxTelegramClient
            self._api_client = HttpxTelegramClient(self._config)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._status = "healthy"

    async def sync_command_menu(self, commands: list[dict]) -> bool:
        if self._api_client is None:
            return False
        return await self._api_client.set_my_commands(commands)

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._status = "stopped"

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        offset: int | None = None
        while True:
            try:
                updates = await self._api_client.get_updates(  # type: ignore[union-attr]
                    offset=offset, timeout=self._config.poll_timeout_s
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redact the bot token: it is embedded in the request URL,
                # so str(exc) (httpx errors include the URL) may leak it.
                _logger.warning(
                    "telegram: getUpdates failed (%s): %s — retrying in %ss",
                    type(exc).__name__,
                    self._redact(str(exc)),
                    _POLL_BACKOFF_S,
                )
                await asyncio.sleep(_POLL_BACKOFF_S)
                continue
            for upd in updates:
                try:
                    await self._dispatch_update(upd)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _logger.warning(
                        "telegram: dispatch_update failed (%s): %s",
                        type(exc).__name__,
                        self._redact(str(exc)),
                    )
            if updates:
                batch_max = max(int(u.get("update_id", 0)) for u in updates)
                offset = max(offset or 0, batch_max + 1)

    def _redact(self, s: str) -> str:
        token = self._config.bot_token
        return s.replace(token, "<token>") if token else s

    # ------------------------------------------------------------------
    # Update dispatch
    # ------------------------------------------------------------------

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        cq = update.get("callback_query")
        if cq is not None:
            await self._handle_callback_query(cq)
            return
        msg = update.get("message") or update.get("edited_message")
        if msg is None:
            return
        text = msg.get("text")
        if not text:
            return
        from_info = msg.get("from") or {}
        user_id_int = from_info.get("id")
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        message_id = msg.get("message_id")

        if not await self._gate(user_id_int, chat_id, text):
            return

        session_id = self._bindings.allocate(chat_id, prefix="tg")
        self._get_or_create_buffer(session_id).chat_id = chat_id

        user_id = str(user_id_int) if user_id_int is not None else ""
        inbound = InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=text,
            user_id=user_id or None,
            metadata={"chat_id": chat_id, "message_id": message_id},
        )
        await self._inbound.put(inbound)

    async def _gate(self, user_id_int: Any, chat_id: str, text: str) -> bool:
        if self._config.allowed_user_ids and user_id_int not in self._config.allowed_user_ids:
            return False
        if self._pairing is None:
            return True
        user_id = str(user_id_int) if user_id_int is not None else ""
        if not user_id:
            return False
        if self._pairing.is_allowed("telegram", user_id):
            return True
        newly_pending = self._pairing.add_pending(
            "telegram", user_id, first_message=text, chat_id=chat_id
        )
        if newly_pending and self._api_client is not None:
            await self._api_client.send_message(
                chat_id=chat_id,
                text=(
                    "您尚未配对。请管理员在服务器上运行:\n"
                    f"xana pair approve telegram {user_id}"
                ),
            )
        return False

    async def _handle_callback_query(self, cq: dict[str, Any]) -> None:
        """Unified card buttons emit:

        - ``/cancel <run_id>`` -> synthesize inbound + ack.
        - ``/attach <run_id>``, ``/help`` etc. -> synthesize inbound + ack.
        - ``__detach__:<run_id>`` -> gateway.detach_foreground (adapter-local).
        """
        data = str(cq.get("data") or "")
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = str(chat.get("id", ""))
        from_info = cq.get("from") or {}
        user_id_int = from_info.get("id")
        cq_id = cq.get("id")

        async def _ack() -> None:
            if self._api_client is not None and cq_id:
                try:
                    await self._api_client.answer_callback_query(
                        callback_query_id=str(cq_id)
                    )
                except Exception:
                    pass

        if not chat_id:
            await _ack()
            return

        if data.startswith("__detach__:"):
            if self._gateway is not None:
                session_id = self._bindings.allocate(chat_id, prefix="tg")
                try:
                    await self._gateway.detach_foreground(session_id)
                except Exception as exc:
                    _logger.debug(
                        "telegram: detach_foreground failed (%s): %s",
                        type(exc).__name__, self._redact(str(exc)),
                    )
            await _ack()
            return

        # All other callback_data values are command strings (e.g. /cancel r1,
        # /attach r1, /help). Run through the same gate as direct messages.
        if data:
            if await self._gate(user_id_int, chat_id, data):
                session_id = self._bindings.allocate(chat_id, prefix="tg")
                self._get_or_create_buffer(session_id).chat_id = chat_id
                user_id = str(user_id_int) if user_id_int is not None else ""
                await self._inbound.put(InboundMessage(
                    adapter_id=self.adapter_id,
                    session_id=session_id,
                    text=data,
                    user_id=user_id or None,
                    metadata={"chat_id": chat_id, "source": "callback_query"},
                ))
        await _ack()

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
        # /attach menu detour: payload may carry attach_choices.
        choices = event.payload.get("attach_choices") if event.payload else None
        if isinstance(choices, list) and choices:
            cands = tuple(
                (str(c.get("run_id") or ""), str(c.get("label") or c.get("run_id") or ""))
                for c in choices
                if c.get("run_id")
            )
            card_id = self._next_menu_card_id(event.session_id, prefix="attach")
            state = build_attach_menu(card_id=card_id, candidates=cands)
            slot = _CardSlot(state=state, terminal=True)
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
            return RenderedMessage(
                adapter_id=self.adapter_id,
                session_id=event.session_id,
                text=slot.state.answer.answer_text if slot.state.answer else "",
                metadata={"event_kind": event.kind, "drop": "terminal"},
            )
        apply_event(slot.state, event)
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
        if buf.chat_id is None or self._api_client is None:
            return
        if not slot.terminal and not finalize:
            slot.state.top_buttons = compute_top_buttons(slot.state)
        else:
            slot.state.top_buttons = ()
        html, kb = render_unified(slot.state)
        markup: dict | None = {"inline_keyboard": kb} if kb else None
        if slot.state.message_id is None:
            try:
                mid = await self._api_client.send_message(
                    chat_id=buf.chat_id, text=html,
                    parse_mode="HTML", reply_markup=markup,
                )
            except Exception as exc:
                _logger.warning(
                    "telegram: send_message failed (%s): %s",
                    type(exc).__name__, self._redact(str(exc)),
                )
                return
            slot.state.message_id = str(mid)
        else:
            try:
                await self._api_client.edit_message_text(
                    chat_id=buf.chat_id,
                    message_id=int(slot.state.message_id),
                    text=html, parse_mode="HTML",
                    reply_markup=markup,
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "message is not modified" in msg:
                    return
                _logger.warning(
                    "telegram: edit_message_text failed (%s): %s — sending new anchor",
                    type(exc).__name__, self._redact(str(exc)),
                )
                try:
                    mid = await self._api_client.send_message(
                        chat_id=buf.chat_id, text=html,
                        parse_mode="HTML", reply_markup=markup,
                    )
                    slot.state.message_id = str(mid)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ProgressEvent path (note_progress)
    # ------------------------------------------------------------------

    def note_progress(self, payload: dict) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        buf = self._get_or_create_buffer(sid)
        kind = payload.get("kind")
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
        pe = ProgressEvent(
            session_id=sid,
            run_id=rid,
            message=str(kind or ""),
            payload=dict(payload),
        )
        apply_event(slot.state, pe)
        finalize = kind in ("ph_replaced", "run_completed_bg")
        if finalize:
            slot.terminal = True
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
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_buffer(self, session_id: str) -> _SessionBuffer:
        if session_id not in self._buffers:
            self._buffers[session_id] = _SessionBuffer(session_id=session_id)
        return self._buffers[session_id]
