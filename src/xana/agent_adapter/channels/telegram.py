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
from xana.agent_adapter.channels.card import Card
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState,
    build_attach_menu,
    build_help_menu,
)
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
        reply_to_message_id: int | None = None,
        reply_quote: str | None = None,
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

    async def delete_webhook(
        self, *, drop_pending_updates: bool = False,
    ) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-card slot
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    session_id: str = ""
    # card_id -> Card. card_id == run_id for PH cards; dedicated id for menus.
    cards: dict[str, Card] = field(default_factory=dict)
    # 待用于下一次新卡片首次 send 的 reply_to(用户原话引用框)。
    pending_reply_to: int | None = None
    # 引用框只显示前 100 字(长消息引用框压屏幕)。
    pending_reply_quote: str | None = None


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
                _logger.warning(
                    "telegram: getUpdates failed (%s): %s — retrying in %ss",
                    type(exc).__name__,
                    self._redact(str(exc)),
                    _POLL_BACKOFF_S,
                )
                await asyncio.sleep(_POLL_BACKOFF_S)
                continue
            if updates:
                _logger.warning(
                    "telegram: got %d update(s) — first kind=%s",
                    len(updates),
                    next(iter(updates[0].keys() - {"update_id"}), "?"),
                )
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
        buf = self._get_or_create_buffer(session_id)
        buf.chat_id = chat_id
        # 记下用户消息 id 和前 100 字引用片段,下一张新卡 send 时设 reply_to
        try:
            buf.pending_reply_to = (
                int(message_id) if message_id is not None else None)
        except (TypeError, ValueError):
            buf.pending_reply_to = None
        buf.pending_reply_quote = (text[:100] if text else None) or None

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

        async def _ack(text: str = "") -> None:
            if self._api_client is not None and cq_id:
                try:
                    await self._api_client.answer_callback_query(
                        callback_query_id=str(cq_id), text=text or None,
                    )
                except Exception:
                    pass

        _STALE_MSG = "此卡片已过期(/clear 后旧会话不可恢复)"

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

        if data.startswith("__load_tools__:"):
            card_id = data.split(":", 1)[1]
            session_id = self._bindings.allocate(chat_id, prefix="tg")
            buf = self._get_or_create_buffer(session_id)
            card = buf.cards.get(card_id)
            if card is not None:
                card.reveal_tools()
                await _ack()
            else:
                await _ack(_STALE_MSG)
            return

        if data.startswith("__page__:"):
            try:
                _, card_id, n_str = data.split(":", 2)
                n = int(n_str)
            except (ValueError, TypeError):
                await _ack()
                return
            session_id = self._bindings.allocate(chat_id, prefix="tg")
            buf = self._get_or_create_buffer(session_id)
            card = buf.cards.get(card_id)
            if card is not None:
                card.set_answer_page(n)
                await _ack()
            else:
                await _ack(_STALE_MSG)
            return

        if data.startswith("__expand__:"):
            # __expand__:<card_id>:<section>
            try:
                _, card_id, section = data.split(":", 2)
            except ValueError:
                await _ack()
                return
            session_id = self._bindings.allocate(chat_id, prefix="tg")
            buf = self._get_or_create_buffer(session_id)
            card = buf.cards.get(card_id)
            if card is not None:
                card.set_expand(section)
                await _ack()
            else:
                await _ack(_STALE_MSG)
            return

        if data.startswith("__expand_page__:"):
            try:
                _, card_id, n_str = data.split(":", 2)
                n = int(n_str)
            except (ValueError, TypeError):
                await _ack()
                return
            session_id = self._bindings.allocate(chat_id, prefix="tg")
            buf = self._get_or_create_buffer(session_id)
            card = buf.cards.get(card_id)
            if card is not None:
                card.set_expand_page(n)
                await _ack()
            else:
                await _ack(_STALE_MSG)
            return

        if data.startswith("__expand_back__:"):
            card_id = data.split(":", 1)[1]
            session_id = self._bindings.allocate(chat_id, prefix="tg")
            buf = self._get_or_create_buffer(session_id)
            card = buf.cards.get(card_id)
            if card is not None:
                card.clear_expand()
                await _ack()
            else:
                await _ack(_STALE_MSG)
            return

        if data == "__noop__":
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
            card = self._make_card(buf, state)
            card.terminal = True
            buf.cards[card_id] = card
            await self._deliver(buf, card)
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
        text_out = card.state.answer.answer_text if card.state.answer else ""
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
            state = UnifiedCardState(card_id=card_id, ph_run_id=ph_run_id)
            card = self._make_card(buf, state)
            buf.cards[card_id] = card
        return card

    def _next_menu_card_id(self, session_id: str, *, prefix: str) -> str:
        n = self._menu_seq.get(session_id, 0) + 1
        self._menu_seq[session_id] = n
        return f"{prefix}-{session_id}-{n}"

    async def _deliver(self, buf: _SessionBuffer, card: Card) -> None:
        """Card 的 deliver:渲染 + send_message / edit_message_text。"""
        if buf.chat_id is None or self._api_client is None:
            return
        html, kb = render_unified(card.state)
        markup: dict | None = {"inline_keyboard": kb} if kb else None
        if card.message_id is None:
            # 首次 send 用 reply_to(显示用户原话 quote 框);随后 edit 不带。
            reply_to = buf.pending_reply_to
            reply_quote = buf.pending_reply_quote
            buf.pending_reply_to = None
            buf.pending_reply_quote = None
            try:
                mid = await self._api_client.send_message(
                    chat_id=buf.chat_id, text=html,
                    parse_mode="MarkdownV2", reply_markup=markup,
                    reply_to_message_id=reply_to,
                    reply_quote=reply_quote,
                )
            except Exception as exc:
                _logger.warning(
                    "telegram: send_message failed (%s): %s",
                    type(exc).__name__, self._redact(str(exc)),
                )
                return
            card.message_id = str(mid)
        else:
            try:
                await self._api_client.edit_message_text(
                    chat_id=buf.chat_id,
                    message_id=int(card.message_id),
                    text=html, parse_mode="MarkdownV2",
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
                        parse_mode="MarkdownV2", reply_markup=markup,
                    )
                    card.message_id = str(mid)
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
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_buffer(self, session_id: str) -> _SessionBuffer:
        if session_id not in self._buffers:
            self._buffers[session_id] = _SessionBuffer(session_id=session_id)
        return self._buffers[session_id]
