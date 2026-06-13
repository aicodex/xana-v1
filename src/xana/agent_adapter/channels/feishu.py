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
from xana.agent_adapter.channels._render_state import RunRenderState, SessionRenderState
from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.feishu_card import (
    build_card,
    build_frozen_ph_card,
    build_pure_answer_card,
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
    # Phase 4 Task 6: run_id → 该 run 是否曾被切到后台(收到过 run_detached)。
    # finalize 时若为 True,渲染纯回答卡(不抢焦点),否则用常规 finalize 卡。
    # Phase 4 / Task 4: 新 PH 状态机已不再设置此字段(由 SessionRenderState 取代);
    # 保留以维持 build_pure_answer_card 现有兼容路径,Task 9 一并清理。
    started_as_detached: dict[str, bool] = field(default_factory=dict)


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
        # PH switch-foreground action
        if str(value.get("action", "")) == "switch_foreground":
            target_run_id = str(value.get("run_id", "") or "")
            if not target_run_id or not chat_id or self._gateway is None:
                return
            session_id = self._bindings.allocate(chat_id, prefix="feishu")
            try:
                await self._gateway.switch_foreground(session_id, target_run_id)
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
        rid_hkind = event.payload.get("run_id") or (
            event.harness_event.run_id if event.harness_event else ""
        )
        if state is not None and rid_hkind and rid_hkind in state.runs and state.runs[rid_hkind].is_frozen:
            # 直接走 draft 累积分支,绕过 coalescer.flush(避免 patch)
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
        if kind in ("run_started", "run_detached", "foreground_changed",
                    "ph_replaced", "run_completed_bg"):
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
        ts.coalescer.flush(self._make_send(buf, ts, finalize=False))

    def _make_send(self, buf: _SessionBuffer, ts: _TurnState, *, finalize: bool):
        async def _send() -> None:
            if buf.chat_id is None or self._api_sender is None:
                return
            # 流式期间无模型按钮 → 合成 ⏹ 停止;finalize 用 extract_buttons 结果
            rows = ts.draft.buttons or ([] if finalize else STOP_BUTTON_ROWS)
            # Phase 4 / Task 4: 全卡带 "切回 <label>" 按钮(若该 session 有后台 run)
            rid = ts.draft.run_id
            state = self._render_states.get(buf.session_id)
            bg = state.bg_runs() if state is not None else None
            # Task 6 旧逻辑:detached run finalize → 纯回答卡(Task 9 计划清理)。
            # 新 PH 状态机的 ph_replaced 分支会单独处理 finalize,这里保持向后兼容。
            if finalize and rid and buf.started_as_detached.get(rid, False):
                card = build_pure_answer_card(ts.draft)
            else:
                card = build_card(
                    ts.draft, finalize=finalize, buttons=rows, bg_runs=bg or None,
                )
            if ts.message_id is None:
                ts.message_id = await self._api_sender.send_card(
                    receive_id=buf.chat_id, card=card
                )
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
            return
        if kind == "run_detached":
            run = state.runs.get(rid)
            if run is None or run.is_frozen:
                return
            state.detach(rid)
            self._schedule(self._patch_frozen(sid, rid, run.label))
            return
        if kind == "foreground_changed":
            prev_fg = state.foreground_run_id
            if rid not in state.runs:
                return
            state.switch_foreground(rid)
            if prev_fg and prev_fg != rid and prev_fg in state.runs:
                prev_run = state.runs[prev_fg]
                self._schedule(self._patch_frozen(sid, prev_fg, prev_run.label))
            self._schedule(self._send_full_card(sid, rid, is_new_message=True))
            return
        if kind == "ph_replaced":
            if rid in state.runs:
                self._schedule(self._send_full_card(sid, rid, is_new_message=False))
            return
        if kind == "run_completed_bg":
            # ph_replaced already finalized; nothing to do.
            return

    def _schedule(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # no running loop (shouldn't happen in production); drop quietly
            coro.close()
            return
        self._pending_tasks.append(task)

    async def _patch_frozen(self, sid: str, rid: str, label: str) -> None:
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        ts = buf.turns.get(rid)
        if ts is None or ts.message_id is None:
            return
        card = build_frozen_ph_card(ts.draft, label)
        await self._api_sender.patch_card(message_id=ts.message_id, card=card)

    async def _send_full_card(
        self, sid: str, rid: str, *, is_new_message: bool
    ) -> None:
        buf = self._buffers.get(sid)
        if buf is None or buf.chat_id is None or self._api_sender is None:
            return
        ts = self._ensure_turn(buf, rid)
        state = self._render_states.get(sid)
        bg = state.bg_runs() if state is not None else None
        # filter out self from bg_runs (foreground should not be in bg list)
        if bg is not None:
            bg = [r for r in bg if r.run_id != rid]
        card = build_card(ts.draft, finalize=False, bg_runs=bg or None)
        if is_new_message or ts.message_id is None:
            ts.message_id = await self._api_sender.send_card(
                receive_id=buf.chat_id, card=card
            )
        else:
            await self._api_sender.patch_card(
                message_id=ts.message_id, card=card
            )

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
