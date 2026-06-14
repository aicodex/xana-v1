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
    TurnDraft,
    build_footer_meta,
    extract_buttons,
    footer_from_progress,
)
from xana.agent_adapter.channels._pairing import PairingStore
from xana.agent_adapter.channels._render_state import BgCardState, SessionRenderState
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.feishu_card import (
    build_bg_placeholder_card,
    build_card,
    build_fg_streaming_card,
)
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


def _bg_panel_md(draft: TurnDraft) -> str:
    """Markdown for the bg card's collapsible content.

    Concatenates thinking + tool blocks + answer into a compact view used by
    the bg placeholder card (Group E dual-card model)."""
    parts: list[str] = []
    if draft.thinking:
        parts.append(f"**思考**\n{draft.thinking}")
    for blk in draft.tool_blocks:
        body = (blk.result or "⏳")[:500]
        parts.append(f"**{blk.header()}**\n{body}")
    if draft.content:
        parts.append(f"**回答**\n{draft.content}")
    return "\n\n".join(parts) if parts else "正在后台运行…"


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
    finalized: bool = False
    last_progress: dict | None = None


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    session_id: str = ""
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
        # Phase 4 / Task 4: PH-driven per-session render state.
        self._render_states: dict[str, SessionRenderState] = {}
        self._pending_tasks: list[asyncio.Task] = []
        # Optional gateway hook for switch_foreground button callbacks (Task 4).
        self._gateway: Any | None = None

    def set_gateway(self, gateway: Any) -> None:
        """Inject a gateway reference so card-action buttons can call
        switch_foreground. Optional; if unset, switch_foreground buttons
        silently no-op on click."""
        self._gateway = gateway

    async def _drain_pending(self) -> None:
        """Test helper: await all scheduled state-machine actions."""
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
        re-enters the gateway as a normal user message. Also handles the
        Phase 4 / Task 4 ``switch_foreground`` action which routes to
        gateway.switch_foreground() instead of the inbound queue."""
        value = (event.get("action") or {}).get("value", {}) or {}
        chat_id = str((event.get("context") or {}).get("open_chat_id", "") or "")
        action = str(value.get("action", "") or "")
        # PH attach action (legacy switch_foreground alias + new attach_run)
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
        # Group E: fg streaming card top controls.
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
        if action == "detach_foreground":
            if not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
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
        # Phase 4 / Task 4: 冻结的后台 run 仍要在 draft 上累积事件
        # (ph_replaced 时一次性出全卡),但不发任何 patch。
        state = self._render_states.get(event.session_id)
        # Dual-card (Group E): only the fg run is allowed to patch its streaming
        # card. Any event whose run_id != fg_run_id accumulates into draft only;
        # ph_replaced will materialize a bg "completed" card later.
        rid_for_freeze = ts.draft.run_id
        if state is not None and state.fg_run_id is not None and rid_for_freeze != state.fg_run_id:
            return await self._render_into_draft_only(event, ts)
        # Legacy fallback for code paths that still set is_frozen without
        # going through the PH state machine (kept for back-compat).
        if state is not None and rid_for_freeze in state.runs and state.runs[rid_for_freeze].is_frozen:
            return await self._render_into_draft_only(event, ts)
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
            # /compact 最终事件携带完整摘要 → 折叠块;普通模型 final 无这两个键,不受影响
            if isinstance(payload.get("thinking"), str) and payload.get("thinking") \
                    and payload.get("thinking_title"):
                ts.draft.thinking = str(payload["thinking"])
                ts.draft.thinking_title = str(payload["thinking_title"])
            ts.draft.content, extracted = extract_buttons(ts.draft.content)
            # payload 行优先,与正文标记解析的结果并存(payload 在前)
            payload_rows = payload.get("buttons")
            ts.draft.buttons = (list(payload_rows) if isinstance(payload_rows, list) else []) + extracted
            usage = event.harness_event.usage if event.harness_event else None
            footer = (
                footer_from_progress(ts.last_progress)
                if ts.last_progress and ts.last_progress.get("kind") == "usage_progress"
                else build_footer_meta(usage)
            )
            if not footer and ts.draft.footer_meta:
                # compact 答案卡: 无 usage — 保留 model,丢上下文(此时不好算)
                footer = [p for p in ts.draft.footer_meta if p[0] == "model"]
            ts.draft.footer_meta = footer
            ts.finalized = True
            await ts.coalescer.finalize(self._make_send(buf, ts, finalize=True))
        elif hkind == "error":
            ts.draft.close_running_tools()
            err = text or str(payload.get("error", "") or "")
            ts.draft.content += f"\n[error] {err}" if err else "\n[error]"
            payload_rows = payload.get("buttons")
            if isinstance(payload_rows, list) and payload_rows:
                ts.draft.buttons = list(payload_rows)
            ts.finalized = True
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
        elif event.harness_event is None and event.payload.get("run_id"):
            # 命令 final/error:无 harness_event,按 gateway 注入的 run_id 落同卡
            run_id = str(event.payload["run_id"])
        elif buf.turns:
            run_id = next(reversed(buf.turns))
        else:
            run_id = "?"
        return self._ensure_turn(buf, run_id)

    def _ensure_turn(self, buf: _SessionBuffer, run_id: str) -> _TurnState:
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
        """AdapterProgressSink hook(sync):usage_progress / compact_progress /
        command_progress 实时刷新当前 turn。_last_progress 只存 usage_progress,
        避免 compact_progress 把新 turn 的 model/context 种子冲掉。

        Phase 4 / Task 4: PH 状态机分支(run_started / run_detached /
        foreground_changed / ph_replaced / run_completed_bg)优先;命中即返回。"""
        kind = payload.get("kind")
        if kind == "usage_progress":
            self._last_progress = dict(payload)
        sid = str(payload.get("session_id") or "")
        if kind in ("run_started", "run_detached", "attached",
                    "foreground_changed", "ph_replaced", "run_completed_bg",
                    "compact_started", "compact_finalized"):
            self._handle_ph_event(sid, payload)
            return
        if kind == "compact_progress" and payload.get("pct") is not None:
            # PH-level compact bar update (Group H). The legacy text-progress
            # path below stays for /compact's draft-rendering.
            self._handle_ph_event(sid, payload)
            return
        buf = self._buffers.get(sid)
        if buf is None:
            return
        if payload.get("kind") == "command_progress":
            # 命令卡始终按 payload.run_id 取/建,绝不落到当前活跃 harness turn(避免冲掉流式正文)
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                return
            ts = self._ensure_turn(buf, run_id)
        else:
            # usage_progress / compact_progress 必须按 run_id 路由,否则它们落到
            # buf.turns 的最后一个 turn(可能已被 detach),会把冻结卡的 footer 刷新
            # → 用户视觉上以为冻结卡又在流。
            run_id = str(payload.get("run_id") or "")
            if run_id and run_id in buf.turns:
                ts = buf.turns[run_id]
            elif run_id:
                ts = self._ensure_turn(buf, run_id)
            else:
                ts = buf.turns[next(reversed(buf.turns))] if buf.turns else None
        if ts is None or ts.finalized:
            # auto-compact 先于新 run 的第一个 harness 事件触发:
            # 用 payload.run_id 提前建 turn,让 压缩中… 立即出卡。
            run_id = str(payload.get("run_id") or "")
            if payload.get("kind") != "compact_progress" or not run_id:
                return
            ts = self._ensure_turn(buf, run_id)
        if payload.get("kind") == "usage_progress":
            ts.last_progress = dict(payload)
        if payload.get("kind") == "command_progress" and payload.get("text"):
            ts.draft.content = str(payload["text"])
        if payload.get("kind") == "compact_progress" and payload.get("text"):
            # /compact 命令流式进度:进度条直接进卡片正文;部分摘要进折叠块
            ts.draft.content = str(payload["text"])
            ts.draft.thinking = str(payload.get("thinking") or "")
            if "thinking_title" in payload:
                ts.draft.thinking_title = payload.get("thinking_title")
        # 进度卡也可带按钮(如 compact 取消按钮);payload 行写进 draft.buttons
        payload_rows = payload.get("buttons")
        if isinstance(payload_rows, list):
            ts.draft.buttons = list(payload_rows)
        ts.draft.footer_meta = footer_from_progress(payload)
        # Dual-card: progress for non-fg runs accumulates into draft only — no
        # patch (the fg card belongs to fg_run_id; bg cards refresh only on
        # ph_replaced).
        state = self._render_states.get(sid)
        rid_p = ts.draft.run_id
        if state is not None and state.fg_run_id is not None and rid_p != state.fg_run_id:
            return
        if state is not None and rid_p in state.runs and state.runs[rid_p].is_frozen:
            return
        ts.coalescer.flush(self._make_send(buf, ts, finalize=False))

    def _make_send(self, buf: _SessionBuffer, ts: _TurnState, *, finalize: bool):
        async def _send() -> None:
            if buf.chat_id is None or self._api_sender is None:
                return
            rid = ts.draft.run_id
            state = self._render_states.get(buf.session_id)
            # Dual-card: only the current fg run patches the fg streaming card.
            # If state machine knows a fg and this isn't it, drop silently —
            # _render_into_draft_only already accumulated the draft.
            if state is not None and state.fg_run_id is not None and rid != state.fg_run_id:
                return
            card = build_fg_streaming_card(ts.draft, finalize=finalize)
            if ts.message_id is None:
                ts.message_id = await self._api_sender.send_card(
                    receive_id=buf.chat_id, card=card
                )
                if state is not None and rid == state.fg_run_id:
                    state.fg_message_id = ts.message_id
            else:
                await self._api_sender.patch_card(message_id=ts.message_id, card=card)
        return _send

    # ------------------------------------------------------------------
    # Phase 4 / Task 4: PH state machine
    # ------------------------------------------------------------------

    async def _render_into_draft_only(
        self, event: AgentEvent, ts: "_TurnState"
    ) -> RenderedMessage:
        """Accumulate event into draft WITHOUT triggering any patch/send.

        Used while a run is frozen (background). The next ph_replaced will
        emit a single full-card patch with the accumulated draft."""
        hkind = self._harness_kind(event)
        payload = event.payload
        text = str(payload.get("text", "") or "")
        if hkind == "model_delta":
            if payload.get("channel") == "thinking":
                ts.draft.append_thinking(text)
            else:
                ts.draft.append_content(text)
        elif hkind == "tool_started":
            ts.draft.start_tool(
                call_id=str(payload.get("call_id", "")),
                name=str(payload.get("tool_name", "")),
                args=payload.get("input", {}) or {},
            )
        elif hkind == "tool_finished":
            ts.draft.finish_tool(
                call_id=str(payload.get("call_id", "")),
                name=str(payload.get("tool_name", "")),
                ok=bool(payload.get("ok", True)),
                result=str(payload.get("text", "")),
                elapsed_ms=None,
            )
        elif hkind in ("final", "error"):
            ts.draft.close_running_tools()
            if text:
                ts.draft.content = text
            ts.finalized = True
        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=ts.draft.content,
            metadata={"event_kind": event.kind, "frozen": True},
        )

    def _handle_ph_event(self, sid: str, payload: dict) -> None:
        kind = payload.get("kind")
        rid = str(payload.get("run_id") or "")
        state = self._render_states.setdefault(sid, SessionRenderState())
        if kind == "run_started":
            label = str(payload.get("label") or rid or "")
            state.start_run(rid, label)
            state.fg_run_id = rid
            state.fg_message_id = None
            return
        if kind == "run_detached":
            run = state.runs.get(rid)
            label = run.label if run is not None else str(payload.get("label") or rid)
            # mark frozen (back-compat field) and clear fg pointer if matches
            if run is not None and not run.is_frozen:
                state.detach(rid)
            was_fg = (state.fg_run_id == rid)
            if was_fg:
                state.fg_run_id = None
                self._schedule(self._patch_fg_frozen(sid, rid))
            # lazy-create bg card (idempotent)
            if rid not in state.bg_cards:
                self._schedule(self._create_bg_card(sid, rid, label))
            return
        if kind in ("attached", "foreground_changed"):
            # New event name 'attached' replaces 'foreground_changed'.
            prev_fg = state.fg_run_id
            label = str(payload.get("label") or "")
            if rid not in state.runs:
                # gateway may have skipped run_started — create a stub so
                # downstream rendering can still find a RunRenderState.
                state.start_run(rid, label or rid)
            # don't use start_run again — manually retarget fg
            if prev_fg and prev_fg != rid:
                prev_label = (
                    state.runs[prev_fg].label if prev_fg in state.runs else prev_fg
                )
                self._schedule(self._patch_fg_frozen(sid, prev_fg))
                if prev_fg not in state.bg_cards:
                    self._schedule(self._create_bg_card(sid, prev_fg, prev_label))
            state.fg_run_id = rid
            state.fg_message_id = None
            # Reset the per-turn message_id so the next render() event sends a
            # fresh fg card (no patching of the prior fg message).
            buf = self._buffers.get(sid)
            if buf is not None and rid in buf.turns:
                buf.turns[rid].message_id = None
                # Schedule an immediate fg card send if we already have a draft.
                if buf.turns[rid].draft.content or buf.turns[rid].draft.tool_blocks \
                        or buf.turns[rid].draft.thinking:
                    self._schedule(self._send_fg_card(sid, rid))
            return
        if kind == "compact_started":
            target_rid = self._most_recent_bg(state)
            crid = rid or f"compact-{len(state.bg_cards)}"
            if target_rid and target_rid in state.bg_cards:
                bg = state.bg_cards[target_rid]
                bg.state = "compacting"
                bg.progress_pct = 0
                state.compact_targets[crid] = target_rid
                self._schedule(self._patch_bg_card(sid, target_rid))
            else:
                # case B: no bg card → create dedicated compact bg card
                self._schedule(self._create_compact_bg_card(sid, payload))
            return
        if kind == "compact_progress":
            target_rid = self._compact_target_rid(state, payload)
            if target_rid and target_rid in state.bg_cards:
                bg = state.bg_cards[target_rid]
                try:
                    pct = int(payload.get("pct", bg.progress_pct))
                except (TypeError, ValueError):
                    pct = bg.progress_pct
                bg.progress_pct = pct
                self._schedule(self._patch_bg_card(sid, target_rid))
            return
        if kind == "compact_finalized":
            target_rid = self._compact_target_rid(state, payload)
            if target_rid and target_rid in state.bg_cards:
                bg = state.bg_cards[target_rid]
                summary = str(payload.get("summary", "") or "")
                if summary:
                    head = f"## 已压缩 {bg.compacted_n + 1} 条对话\n{summary}\n\n---\n"
                    bg.panel_md = head + (bg.panel_md or "")
                    bg.compacted_n += 1
                bg.state = "running" if state.fg_run_id != target_rid else bg.state
                if payload.get("cancelled"):
                    bg.state = "cancelled"
                elif payload.get("error"):
                    bg.state = "failed"
                bg.progress_pct = 100
                self._schedule(self._patch_bg_card(sid, target_rid))
                # forget the compact->target mapping
                crid = str(payload.get("run_id") or "")
                state.compact_targets.pop(crid, None)
            return
        if kind == "ph_replaced":
            # Run finished. If a bg card exists for it, patch to completed.
            # If not (run finished as fg), the coalescer's final patch already
            # finalized the fg card — noop here.
            if rid in state.bg_cards:
                self._schedule(self._complete_bg_card(sid, rid))
            return
        if kind == "run_completed_bg":
            # ph_replaced already handled it; noop.
            return

    def _schedule(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # no running loop (shouldn't happen in production); drop quietly
            coro.close()
            return
        self._pending_tasks.append(task)

    async def _patch_fg_frozen(self, sid: str, rid: str) -> None:
        """Patch the prior fg streaming card to its frozen look (no top
        controls). After this, the card is no longer touched."""
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        ts = buf.turns.get(rid)
        if ts is None or ts.message_id is None:
            return
        card = build_fg_streaming_card(ts.draft, finalize=True)
        await self._api_sender.patch_card(message_id=ts.message_id, card=card)

    async def _create_bg_card(self, sid: str, rid: str, label: str) -> None:
        """Lazily create the bg placeholder card for a detached run.
        Idempotent: skips if a card already exists for this run_id."""
        state = self._render_states.get(sid)
        if state is None or rid in state.bg_cards:
            return
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        ts = buf.turns.get(rid)
        draft = ts.draft if ts is not None else TurnDraft(run_id=rid)
        panel_md = _bg_panel_md(draft)
        card = build_bg_placeholder_card(
            label=label, state="running", progress_pct=0, panel_md=panel_md,
        )
        mid = await self._api_sender.send_card(receive_id=buf.chat_id, card=card)
        state.bg_cards[rid] = BgCardState(
            message_id=mid, label=label, state="running", progress_pct=0,
            panel_md=panel_md,
        )

    async def _complete_bg_card(self, sid: str, rid: str) -> None:
        state = self._render_states.get(sid)
        if state is None:
            return
        bg = state.bg_cards.get(rid)
        if bg is None or self._api_sender is None:
            return
        buf = self._buffers.get(sid)
        ts = buf.turns.get(rid) if buf is not None else None
        draft = ts.draft if ts is not None else TurnDraft(run_id=rid)
        bg.state = "completed"
        bg.progress_pct = 100
        bg.panel_md = _bg_panel_md(draft)
        card = build_bg_placeholder_card(
            label=bg.label, state=bg.state, progress_pct=bg.progress_pct,
            panel_md=bg.panel_md, compacted_n=bg.compacted_n,
        )
        await self._api_sender.patch_card(message_id=bg.message_id, card=card)

    def _most_recent_bg(self, state: SessionRenderState) -> str | None:
        """Return the run_id of the most recently created bg card.
        Python dicts preserve insertion order → last key inserted is newest."""
        if not state.bg_cards:
            return None
        return next(reversed(state.bg_cards))

    def _compact_target_rid(
        self, state: SessionRenderState, payload: dict
    ) -> str | None:
        """Resolve the bg card run_id that a compact_* event lands on.
        Looks up state.compact_targets via the compact's own run_id."""
        crid = str(payload.get("run_id") or "")
        if not crid:
            return self._most_recent_bg(state)
        return state.compact_targets.get(crid) or (
            crid if crid in state.bg_cards else None
        )

    async def _patch_bg_card(self, sid: str, rid: str) -> None:
        """Re-render a bg card from current BgCardState."""
        state = self._render_states.get(sid)
        if state is None or self._api_sender is None:
            return
        bg = state.bg_cards.get(rid)
        if bg is None:
            return
        card = build_bg_placeholder_card(
            label=bg.label, state=bg.state, progress_pct=bg.progress_pct,
            panel_md=bg.panel_md or " ", compacted_n=bg.compacted_n,
        )
        await self._api_sender.patch_card(message_id=bg.message_id, card=card)

    async def _create_compact_bg_card(self, sid: str, payload: dict) -> None:
        """Case B: no existing bg card → spin up a compact-only bg card."""
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        state = self._render_states.setdefault(sid, SessionRenderState())
        crid = str(payload.get("run_id") or f"compact-{len(state.bg_cards)}")
        label = str(payload.get("label") or "compact")
        card = build_bg_placeholder_card(
            label=label, state="compacting", progress_pct=0, panel_md=" ",
        )
        mid = await self._api_sender.send_card(receive_id=buf.chat_id, card=card)
        # bg card key = compact run_id itself (no prior detached run to land on)
        state.bg_cards[crid] = BgCardState(
            message_id=mid, label=label,
            state="compacting", progress_pct=0, panel_md=" ",
        )
        state.compact_targets[crid] = crid

    async def _send_fg_card(self, sid: str, rid: str) -> None:
        """Send a fresh fg streaming card from current draft (used on attach
        to make the switch visible immediately)."""
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        ts = self._ensure_turn(buf, rid)
        state = self._render_states.get(sid)
        if state is None or state.fg_run_id != rid:
            return
        if ts.message_id is not None:
            return  # already sent
        card = build_fg_streaming_card(ts.draft, finalize=False)
        ts.message_id = await self._api_sender.send_card(
            receive_id=buf.chat_id, card=card,
        )
        state.fg_message_id = ts.message_id

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
