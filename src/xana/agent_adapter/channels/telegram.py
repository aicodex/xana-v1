"""TelegramAdapter: Telegram channel adapter via Bot API long-polling.

Polls getUpdates, allocates session bindings, and renders AgentEvents back as
Telegram messages that are edited in-place while streaming (draft → edit_message_text).

Footer meta on finalize mirrors FeishuAdapter: pulls from harness_event.usage
when present.  Final payload["text"] is the full cumulative text (per
harness_sdk/default.py semantics), so final does assignment not append.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._throttle import Throttle
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


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
        self, *, chat_id: str, text: str, parse_mode: str | None = None
    ) -> int:  # pragma: no cover
        raise NotImplementedError

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_updates(
        self, *, offset: int | None, timeout: int
    ) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuffer:
    cumulative_text: str = ""
    message_id: int | None = None
    throttle: Throttle = field(default_factory=lambda: Throttle(min_interval_s=0.25))
    chat_id: str | None = None


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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._api_client is None:
            from xana.agent_adapter.channels.telegram_client import HttpxTelegramClient
            self._api_client = HttpxTelegramClient(self._config)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._status = "healthy"

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
            except Exception:
                await asyncio.sleep(1)
                continue
            for upd in updates:
                await self._dispatch_update(upd)
            if updates:
                batch_max = max(int(u.get("update_id", 0)) for u in updates)
                offset = max(offset or 0, batch_max + 1)

    # ------------------------------------------------------------------
    # Update dispatch
    # ------------------------------------------------------------------

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if msg is None:
            return
        text = msg.get("text")
        if not text:
            return
        from_info = msg.get("from") or {}
        user_id_int = from_info.get("id")
        if self._config.allowed_user_ids and user_id_int not in self._config.allowed_user_ids:
            return
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        message_id = msg.get("message_id")

        user_id = str(user_id_int) if user_id_int is not None else ""
        if self._pairing is not None and not user_id:
            return
        if self._pairing is not None and not self._pairing.is_allowed("telegram", user_id):
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
            return

        session_id = self._bindings.allocate(chat_id, prefix="tg")
        self._get_or_create_buffer(session_id).chat_id = chat_id

        inbound = InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=text,
            user_id=user_id or None,
            metadata={"chat_id": chat_id, "message_id": message_id},
        )
        await self._inbound.put(inbound)

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
        """Explicitly map chat_id → session_id and create a buffer."""
        self._bindings.put(chat_id, session_id)
        self._get_or_create_buffer(session_id).chat_id = chat_id

    async def bind_session(self, binding: SessionBinding) -> None:
        """Called by gateway with SessionBinding; maps external_thread_id → session."""
        chat_id = binding.external_thread_id
        session_id = binding.session_id
        self._bindings.put(chat_id, session_id)
        self._get_or_create_buffer(session_id).chat_id = chat_id

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    async def render(self, event: AgentEvent) -> RenderedMessage:
        kind = event.kind
        text = str(event.payload.get("text", ""))
        buf = self._get_or_create_buffer(event.session_id)

        if kind == "draft":
            buf.cumulative_text += text
            await self._send_or_edit(buf, finalize=False)
        elif kind == "final":
            if text:
                buf.cumulative_text = text
            footer = self._build_footer(event)
            divider = "\n\n――――――――――――"
            if footer:
                buf.cumulative_text += f"{divider}\n_{footer}_"
            else:
                buf.cumulative_text += divider
            await self._send_or_edit(buf, finalize=True)
        elif kind == "error":
            buf.cumulative_text += f"\n[error] {text}" if text else "\n[error]"
            await self._send_or_edit(buf, finalize=True)

        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=buf.cumulative_text,
            metadata={"event_kind": kind},
        )

    def _build_footer(self, event: AgentEvent) -> str:
        usage = getattr(event.harness_event, "usage", None) if event.harness_event else None
        if usage is None:
            return ""
        parts: list[str] = []
        if usage.model:
            parts.append(f"model: {usage.model}")
        tin = usage.exact_input_tokens
        tout = usage.exact_output_tokens
        if tin is not None or tout is not None:
            parts.append(f"tokens: {tin or 0} in / {tout or 0} out")
        return " | ".join(parts)

    async def _send_or_edit(self, buf: _SessionBuffer, *, finalize: bool) -> None:  # noqa: ARG002
        if buf.chat_id is None or self._api_client is None:
            return
        await buf.throttle.wait()
        display_text = buf.cumulative_text or "…"
        if buf.message_id is None:
            mid = await self._api_client.send_message(
                chat_id=buf.chat_id, text=display_text, parse_mode="Markdown"
            )
            buf.message_id = mid
        else:
            await self._api_client.edit_message_text(
                chat_id=buf.chat_id,
                message_id=buf.message_id,
                text=display_text,
                parse_mode="Markdown",
            )

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
            self._buffers[session_id] = _SessionBuffer()
        return self._buffers[session_id]
