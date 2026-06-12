"""FeishuAdapter: Feishu (Lark) channel adapter.

Receives im.message.receive_v1 events via a webhook (or direct _on_event
injection in tests), allocates session bindings, and renders AgentEvents
back as interactive card messages that are patched in-place while streaming.

Footer meta on finalize mirrors CliAdapter's _collect_footer_meta: pulls
from harness_event.usage when present.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.feishu_events import parse_message_event
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
class FeishuConfig:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str | None = None
    api_base: str = "https://open.feishu.cn"
    bot_open_id: str | None = None
    connection_mode: str = "ws"  # "ws" (WebSocket long-connection) | "webhook"


# ---------------------------------------------------------------------------
# FeishuApiSender Protocol (structural)
# ---------------------------------------------------------------------------


class FeishuApiSender:
    """Structural protocol — any object with these two async methods qualifies."""

    async def send_card(self, *, receive_id: str, card: dict) -> str:  # pragma: no cover
        raise NotImplementedError

    async def patch_card(self, *, message_id: str, card: dict) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class _SessionBuffer:
    cumulative_text: str = ""
    last_message_id: str | None = None
    coalescer: DraftCoalescer = field(default_factory=lambda: DraftCoalescer(min_interval_s=0.25))
    chat_id: str | None = None


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------


def _build_card(text: str, footer_meta: list[tuple[str, str]], finalize: bool) -> dict:
    """Build a Feishu interactive card (schema 2.0) with optional footer.

    On finalize we always emit a divider (hr) so the test and UI both get a
    clear visual end-of-turn marker, even when there is no usage metadata.
    """
    elements: list[dict] = [{"tag": "markdown", "content": text}]
    if finalize:
        elements.append({"tag": "hr"})
        if footer_meta:
            meta_line = " | ".join(f"{k}: {v}" for k, v in footer_meta)
            elements.append({
                "tag": "markdown",
                "content": f"<font color='grey'>{meta_line}</font>",
            })
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "body": {"elements": elements},
    }


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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._api_sender is None:
            from xana.agent_adapter.channels.feishu_webhook import build_real_sender
            self._api_sender = build_real_sender(self._config)
        if self._config.connection_mode == "ws":
            # Lazy import keeps lark-oapi an optional dep for webhook mode.
            from xana.agent_adapter.channels.feishu_ws import FeishuWSClient
            self._ws_client = FeishuWSClient(
                self._config.app_id,
                self._config.app_secret,
                domain=self._config.api_base,
                on_im_message=self._on_event,
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
        """Parse a Feishu v2 event dict and enqueue an InboundMessage."""
        parsed = parse_message_event(event)
        if parsed is None:
            return
        sender_id = parsed.sender_open_id or ""
        if self._pairing is not None and not sender_id:
            return
        if self._pairing is not None and not self._pairing.is_allowed("feishu", sender_id):
            newly_pending = self._pairing.add_pending(
                "feishu", sender_id, first_message=parsed.text, chat_id=parsed.chat_id
            )
            if newly_pending and self._api_sender is not None:
                hint = (
                    "您尚未配对。请管理员运行: "
                    f"`xana pair approve feishu {sender_id}`"
                )
                card = _build_card(hint, [], True)
                await self._api_sender.send_card(receive_id=parsed.chat_id, card=card)
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
            # Non-blocking: latest cumulative_text wins when the send fires.
            buf.coalescer.flush(
                lambda: self._patch_or_send_now(buf, finalize=False, footer_meta=[])
            )
        elif kind == "final":
            if text:
                # final payload["text"] is the full cumulative text joined by "\n"
                # (set by harness_sdk/default.py final_text = "\n".join(cumulative_text))
                buf.cumulative_text = text
            footer_meta = self._collect_footer_meta(event)
            await buf.coalescer.finalize(
                lambda: self._patch_or_send_now(buf, finalize=True, footer_meta=footer_meta)
            )
        elif kind == "error":
            buf.cumulative_text += f"\n[error] {text}" if text else "\n[error]"
            await buf.coalescer.finalize(
                lambda: self._patch_or_send_now(buf, finalize=True, footer_meta=[])
            )

        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=buf.cumulative_text,
            metadata={"event_kind": kind},
        )

    def _collect_footer_meta(self, event: AgentEvent) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        usage = getattr(event.harness_event, "usage", None) if event.harness_event else None
        if usage is not None:
            if usage.model:
                pairs.append(("model", str(usage.model)))
            tin = usage.exact_input_tokens
            tout = usage.exact_output_tokens
            if tin is not None or tout is not None:
                pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
        return pairs

    async def _patch_or_send_now(
        self,
        buf: _SessionBuffer,
        *,
        finalize: bool,
        footer_meta: list[tuple[str, str]],
    ) -> None:
        """Raw API call: first message is a send, later ones patch in place."""
        if buf.chat_id is None or self._api_sender is None:
            return
        card = _build_card(buf.cumulative_text, footer_meta, finalize)
        if buf.last_message_id is None:
            mid = await self._api_sender.send_card(receive_id=buf.chat_id, card=card)
            buf.last_message_id = mid
        else:
            await self._api_sender.patch_card(message_id=buf.last_message_id, card=card)

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
            self._buffers[session_id] = _SessionBuffer()
        return self._buffers[session_id]
