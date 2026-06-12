"""FeishuAdapter: Feishu (Lark) channel adapter.

Receives im.message.receive_v1 events via a webhook (or direct _on_event
injection in tests), allocates session bindings, and renders AgentEvents
back as interactive card messages that are patched in-place while streaming.

渲染侧是 per-run 卡片状态机:每个 run_id 一份 _TurnState(TurnDraft + 自己的
message_id + 自己的 DraftCoalescer),事件按 harness_event.run_id 路由到自己
的气泡;新轮开新卡,交错/晚到的旧轮事件 patch 旧卡,互不覆盖。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels._draft import (
    STOP_BUTTON_ROWS,
    TurnDraft,
    build_footer_meta,
    extract_buttons,
    footer_from_progress,
)
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.feishu_card import build_card
from xana.agent_adapter.channels.feishu_events import parse_message_event
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent

_MAX_TURNS_KEPT = 4


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
# Per-session / per-run state
# ---------------------------------------------------------------------------


@dataclass
class _TurnState:
    draft: TurnDraft
    message_id: str | None = None
    coalescer: DraftCoalescer = field(default_factory=lambda: DraftCoalescer(min_interval_s=0.25))
    tool_started_ts: dict = field(default_factory=dict)


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    turns: "OrderedDict[str, _TurnState]" = field(default_factory=OrderedDict)


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
        self._last_progress: dict[str, Any] | None = None

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
        """Parse a Feishu v2 event dict and enqueue an InboundMessage."""
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
        """Pairing gate. True = allowed; False = blocked (user added to
        pending and hint card sent if newly pending)."""
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
        """Handle a converted `card.action.trigger` dict: a button click
        re-enters the gateway as a normal user message."""
        send_text = str((event.get("action") or {}).get("value", {}).get("send_text", "") or "")
        open_id = str((event.get("operator") or {}).get("open_id", "") or "")
        chat_id = str((event.get("context") or {}).get("open_chat_id", "") or "")
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
        buf = self._get_or_create_buffer(event.session_id)
        ts = self._turn_for(buf, event)
        hkind = self._harness_kind(event)
        payload = event.payload
        text = str(payload.get("text", "") or "")

        if hkind == "model_delta":
            if payload.get("channel") == "thinking":
                ts.draft.append_thinking(text)
            else:
                ts.draft.append_content(text)
            ts.coalescer.flush(self._make_send(buf, ts, finalize=False))
        elif hkind == "tool_started":
            call_id = str(payload.get("call_id", ""))
            ts.draft.start_tool(
                call_id=call_id,
                name=str(payload.get("tool_name", "")),
                args=payload.get("input", {}) or {},
            )
            ts.tool_started_ts[call_id] = time.monotonic()
            ts.coalescer.flush(self._make_send(buf, ts, finalize=False))
        elif hkind == "tool_finished":
            call_id = str(payload.get("call_id", ""))
            started = ts.tool_started_ts.pop(call_id, None)
            elapsed_ms = int((time.monotonic() - started) * 1000) if started is not None else None
            # call_id 失配的同名兜底在 TurnDraft.finish_tool 内部完成
            ts.draft.finish_tool(
                call_id=call_id,
                name=str(payload.get("tool_name", "")),
                ok=bool(payload.get("ok", True)),
                result=str(payload.get("text", "")),
                elapsed_ms=elapsed_ms,
            )
            ts.coalescer.flush(self._make_send(buf, ts, finalize=False))
        elif hkind == "final":
            ts.draft.close_running_tools()
            if text:
                ts.draft.content = text
            ts.draft.content, ts.draft.buttons = extract_buttons(ts.draft.content)
            usage = event.harness_event.usage if event.harness_event else None
            ts.draft.footer_meta = build_footer_meta(usage)
            await ts.coalescer.finalize(self._make_send(buf, ts, finalize=True))
        elif hkind == "error":
            ts.draft.close_running_tools()
            err = text or str(payload.get("error", "") or "")
            ts.draft.content += f"\n[error] {err}" if err else "\n[error]"
            await ts.coalescer.finalize(self._make_send(buf, ts, finalize=True))
        # 其他 harness kind(turn_started/paused/cancelled 等):不渲染。

        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=ts.draft.content,
            metadata={"event_kind": event.kind},
        )

    @staticmethod
    def _harness_kind(event: AgentEvent) -> str:
        if event.harness_event is not None:
            return event.harness_event.kind
        # 无 harness_event 时由 AgentEvent.kind 推断
        return {"draft": "model_delta", "tool": "tool_finished",
                "final": "final", "error": "error"}.get(event.kind, event.kind)

    def _turn_for(self, buf: _SessionBuffer, event: AgentEvent) -> _TurnState:
        if event.harness_event is not None and event.harness_event.run_id:
            run_id = event.harness_event.run_id
        elif buf.turns:
            run_id = next(reversed(buf.turns))
        else:
            run_id = "?"
        ts = buf.turns.get(run_id)
        if ts is None:
            ts = _TurnState(draft=TurnDraft(run_id=run_id))
            # 脚标常驻:turn 第一个事件就带上次已知的 model/context
            if self._last_progress is not None:
                ts.draft.footer_meta = footer_from_progress(self._last_progress)
            buf.turns[run_id] = ts
            while len(buf.turns) > _MAX_TURNS_KEPT:
                buf.turns.popitem(last=False)
        return ts

    def note_progress(self, payload: dict) -> None:
        """AdapterProgressSink hook(sync):usage_progress 实时刷新当前 turn 脚标。"""
        self._last_progress = dict(payload)
        buf = self._buffers.get(str(payload.get("session_id") or ""))
        if buf is None or not buf.turns:
            return
        ts = buf.turns[next(reversed(buf.turns))]
        ts.draft.footer_meta = footer_from_progress(payload)
        ts.coalescer.flush(self._make_send(buf, ts, finalize=False))

    def _make_send(self, buf: _SessionBuffer, ts: _TurnState, *, finalize: bool):
        async def _send() -> None:
            if buf.chat_id is None or self._api_sender is None:
                return
            # 流式期间无模型按钮 → 合成 ⏹ 停止;finalize 用 extract_buttons 结果
            rows = ts.draft.buttons or ([] if finalize else STOP_BUTTON_ROWS)
            card = build_card(ts.draft, finalize=finalize, buttons=rows)
            if ts.message_id is None:
                ts.message_id = await self._api_sender.send_card(
                    receive_id=buf.chat_id, card=card
                )
            else:
                await self._api_sender.patch_card(message_id=ts.message_id, card=card)
        return _send

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
