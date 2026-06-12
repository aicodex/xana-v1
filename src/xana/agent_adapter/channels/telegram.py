"""TelegramAdapter: Telegram channel adapter via Bot API long-polling.

Polls getUpdates, allocates session bindings, and renders AgentEvents back
as Telegram HTML messages.

渲染侧是 per-run 状态机(同 feishu.py Task 5):每个 run_id 一份 _TurnState
(TurnDraft + 流式 message_id + 自己的 DraftCoalescer),事件按
harness_event.run_id 路由;新轮开新气泡。Telegram 特有形态:

- 流式 = 单消息原地 edit 增长;
- 工具块 = 独立 sealed 消息(tool_started 立即 send ⏳ 占位,不走 coalescer;
  tool_finished 对同 call_id 的占位 edit 成结果);finalize 不再重发工具块;
- finalize 拆分(yuxu adapters/telegram.py:313-395):有 thinking → 流式消息
  edit 成 thinking 气泡,answer(正文+脚标+按钮)新 send;无 thinking →
  流式消息原地 edit 成 answer;answer 空且有流式消息 → edit 成 " " 清殻。
  final 路径只渲染一次,严防 yuxu 2026-05-29 的重复 answer bug;
- 按钮 = inline keyboard,callback_data='btn:{r}:{c}'(64 字节限制),真实
  send 文本存 _TurnState.last_buttons,callback_query 反查后合成
  InboundMessage 入队(与普通消息共用 pairing/allowed gate)。
"""
from __future__ import annotations

import asyncio
import logging
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
from xana.agent_adapter.channels.telegram_html import (
    buttons_to_reply_markup,
    render_answer,
    render_streaming,
    render_thinking,
    render_tool_block,
)
from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent

_logger = logging.getLogger(__name__)

# Backoff after a getUpdates failure. Module constant so tests can zero it.
_POLL_BACKOFF_S = 3.0

_MAX_TURNS_KEPT = 4


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


# ---------------------------------------------------------------------------
# Per-session / per-run state
# ---------------------------------------------------------------------------


@dataclass
class _TurnState:
    draft: TurnDraft
    message_id: int | None = None  # 流式消息(后被 finalize 改写)
    coalescer: DraftCoalescer = field(default_factory=lambda: DraftCoalescer(min_interval_s=0.25))
    tool_msg_ids: dict[str, int] = field(default_factory=dict)  # call_id → sealed 消息
    tool_started_ts: dict = field(default_factory=dict)
    last_buttons: list[list[dict]] = field(default_factory=list)
    finalized: bool = False
    last_progress: dict | None = None


@dataclass
class _SessionBuffer:
    chat_id: str | None = None
    turns: "OrderedDict[str, _TurnState]" = field(default_factory=OrderedDict)


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
        self._last_progress: dict[str, Any] | None = None

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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redact the bot token: it is embedded in the request URL,
                # so str(exc) (httpx errors include the URL) may leak it.
                # No .exception()/traceback either — tracebacks embed the URL.
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
                    # One bad update must not kill the poll loop.
                    _logger.warning(
                        "telegram: dispatch_update failed (%s): %s",
                        type(exc).__name__,
                        self._redact(str(exc)),
                    )
            if updates:
                batch_max = max(int(u.get("update_id", 0)) for u in updates)
                offset = max(offset or 0, batch_max + 1)

    def _redact(self, s: str) -> str:
        """Replace the bot token with a placeholder before logging."""
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
        """allowed_user_ids + pairing gate, shared by messages and
        callback_query. True = allowed."""
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
        """按钮点击:btn:{r}:{c} 反查该 session 最新一轮的真实 send 文本,
        过与普通消息一致的 gate,合成 InboundMessage 入队。"""
        data = str(cq.get("data") or "")
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = str(chat.get("id", ""))
        send_text = self._resolve_button_text(chat_id, data) if chat_id else None
        if send_text:
            user_id_int = (cq.get("from") or {}).get("id")
            if await self._gate(user_id_int, chat_id, send_text):
                session_id = self._bindings.allocate(chat_id, prefix="tg")
                self._get_or_create_buffer(session_id).chat_id = chat_id
                user_id = str(user_id_int) if user_id_int is not None else ""
                await self._inbound.put(InboundMessage(
                    adapter_id=self.adapter_id,
                    session_id=session_id,
                    text=send_text,
                    user_id=user_id or None,
                    metadata={"chat_id": chat_id, "source": "callback_query"},
                ))
        # 必须应答,否则客户端按钮一直转圈;失败仅 debug 级吞掉。
        if self._api_client is not None and cq.get("id"):
            try:
                await self._api_client.answer_callback_query(
                    callback_query_id=str(cq["id"])
                )
            except Exception as exc:
                _logger.debug(
                    "telegram: answerCallbackQuery failed (%s): %s",
                    type(exc).__name__, self._redact(str(exc)),
                )

    def _resolve_button_text(self, chat_id: str, data: str) -> str | None:
        if not data.startswith("btn:"):
            return None
        try:
            _, r_s, c_s = data.split(":", 2)
            r, c = int(r_s), int(c_s)
        except ValueError:
            return None
        if r < 0 or c < 0:
            return None
        session_id = self._bindings.allocate(chat_id, prefix="tg")
        buf = self._buffers.get(session_id)
        if buf is None:
            return None
        for ts in reversed(buf.turns.values()):
            if ts.last_buttons:
                try:
                    return str(ts.last_buttons[r][c]["send"])
                except (IndexError, KeyError):
                    return None
        return None

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
            ts.coalescer.flush(self._make_stream_send(buf, ts))
        elif hkind == "tool_started":
            call_id = str(payload.get("call_id", ""))
            ts.draft.start_tool(
                call_id=call_id,
                name=str(payload.get("tool_name", "")),
                args=payload.get("input", {}) or {},
            )
            ts.tool_started_ts[call_id] = time.monotonic()
            # sealed 占位消息:立即 send,不走 coalescer
            await self._send_tool_message(buf, ts, call_id)
        elif hkind == "tool_finished":
            call_id = str(payload.get("call_id", ""))
            started = ts.tool_started_ts.pop(call_id, None)
            elapsed_ms = int((time.monotonic() - started) * 1000) if started is not None else None
            # call_id 失配的同名兜底在 TurnDraft.finish_tool 内部完成;
            # 用返回块的 call_id 回查占位 sealed 消息再 edit。
            blk = ts.draft.finish_tool(
                call_id=call_id,
                name=str(payload.get("tool_name", "")),
                ok=bool(payload.get("ok", True)),
                result=str(payload.get("text", "")),
                elapsed_ms=elapsed_ms,
            )
            await self._send_tool_message(buf, ts, blk.call_id)
        elif hkind == "final":
            for blk in ts.draft.close_running_tools():
                await self._send_tool_message(buf, ts, blk.call_id)
            if text:
                ts.draft.content = text
            ts.draft.content, ts.draft.buttons = extract_buttons(ts.draft.content)
            ts.last_buttons = ts.draft.buttons
            usage = event.harness_event.usage if event.harness_event else None
            ts.draft.footer_meta = (
                footer_from_progress(ts.last_progress)
                if ts.last_progress and ts.last_progress.get("kind") == "usage_progress"
                else build_footer_meta(usage)
            )
            ts.finalized = True
            await ts.coalescer.finalize(self._make_finalize_send(buf, ts))
        elif hkind == "error":
            for blk in ts.draft.close_running_tools():
                await self._send_tool_message(buf, ts, blk.call_id)
            err = text or str(payload.get("error", "") or "")
            ts.draft.content += f"\n[error] {err}" if err else "\n[error]"
            ts.finalized = True
            await ts.coalescer.finalize(self._make_finalize_send(buf, ts))
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
        """AdapterProgressSink hook(sync):usage_progress / compact_progress
        实时刷新当前 turn 脚标。_last_progress 只存 usage_progress,
        避免 compact_progress 把新 turn 的 model/context 种子冲掉。"""
        if payload.get("kind") == "usage_progress":
            self._last_progress = dict(payload)
        buf = self._buffers.get(str(payload.get("session_id") or ""))
        if buf is None:
            return
        ts = buf.turns[next(reversed(buf.turns))] if buf.turns else None
        if ts is None or ts.finalized:
            # auto-compact 先于新 run 的第一个 harness 事件触发:
            # 用 payload.run_id 提前建 turn,让 压缩中… 立即出消息。
            run_id = str(payload.get("run_id") or "")
            if payload.get("kind") != "compact_progress" or not run_id:
                return
            ts = self._ensure_turn(buf, run_id)
        if payload.get("kind") == "usage_progress":
            ts.last_progress = dict(payload)
        if payload.get("kind") == "compact_progress" and payload.get("text"):
            # /compact 命令流式进度:进度条直接进消息正文
            ts.draft.content = str(payload["text"])
        ts.draft.footer_meta = footer_from_progress(payload)
        ts.coalescer.flush(self._make_stream_send(buf, ts))

    # -- outbound senders ------------------------------------------------

    def _make_stream_send(self, buf: _SessionBuffer, ts: _TurnState):
        async def _send() -> None:
            if buf.chat_id is None or self._api_client is None:
                return
            html = render_streaming(ts.draft)
            # 流式期间挂 ⏹ 停止按钮;finalize 路径会换成模型提取的选项按钮。
            # 同步存入 last_buttons,callback 'btn:0:0' 才能反查到 "/cancel"。
            ts.last_buttons = STOP_BUTTON_ROWS
            markup = buttons_to_reply_markup(STOP_BUTTON_ROWS)
            if ts.message_id is None:
                ts.message_id = await self._api_client.send_message(
                    chat_id=buf.chat_id, text=html, parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                await self._api_client.edit_message_text(
                    chat_id=buf.chat_id, message_id=ts.message_id,
                    text=html, parse_mode="HTML", reply_markup=markup,
                )
        return _send

    async def _send_tool_message(
        self, buf: _SessionBuffer, ts: _TurnState, call_id: str
    ) -> None:
        """工具块 = 独立 sealed 消息:占位 send 一次,结果对同 call_id edit。"""
        if buf.chat_id is None or self._api_client is None:
            return
        blk = next(
            (b for b in reversed(ts.draft.tool_blocks) if b.call_id == call_id), None
        )
        if blk is None:
            return
        html = render_tool_block(blk)
        mid = ts.tool_msg_ids.get(call_id)
        try:
            if mid is None:
                mid = await self._api_client.send_message(
                    chat_id=buf.chat_id, text=html, parse_mode="HTML"
                )
                ts.tool_msg_ids[call_id] = mid
            else:
                await self._api_client.edit_message_text(
                    chat_id=buf.chat_id, message_id=mid, text=html, parse_mode="HTML"
                )
        except Exception as exc:
            _logger.warning(
                "telegram: _send_tool_message failed (%s): %s",
                type(exc).__name__,
                self._redact(str(exc)),
            )

    def _make_finalize_send(self, buf: _SessionBuffer, ts: _TurnState):
        """finalize 拆分(yuxu :313-395)。answer 在此恰好 send/edit 一次;
        工具块已 live sealed,不重发。"""
        async def _send() -> None:
            if buf.chat_id is None or self._api_client is None:
                return
            markup = buttons_to_reply_markup(ts.draft.buttons)
            answer = render_answer(ts.draft)
            has_answer = bool(answer.strip())
            if ts.draft.thinking:
                thinking_html = render_thinking(ts.draft.thinking)
                if ts.message_id is not None:
                    await self._api_client.edit_message_text(
                        chat_id=buf.chat_id, message_id=ts.message_id,
                        text=thinking_html, parse_mode="HTML",
                    )
                else:
                    ts.message_id = await self._api_client.send_message(
                        chat_id=buf.chat_id, text=thinking_html, parse_mode="HTML"
                    )
                if has_answer:
                    await self._api_client.send_message(
                        chat_id=buf.chat_id, text=answer,
                        parse_mode="HTML", reply_markup=markup,
                    )
            else:
                if has_answer and ts.message_id is not None:
                    await self._api_client.edit_message_text(
                        chat_id=buf.chat_id, message_id=ts.message_id,
                        text=answer, parse_mode="HTML", reply_markup=markup,
                    )
                elif has_answer:
                    ts.message_id = await self._api_client.send_message(
                        chat_id=buf.chat_id, text=answer,
                        parse_mode="HTML", reply_markup=markup,
                    )
                elif ts.message_id is not None:
                    # 没东西可展示但留着流式殻 → 清殻。
                    await self._api_client.edit_message_text(
                        chat_id=buf.chat_id, message_id=ts.message_id,
                        text=" ", parse_mode="HTML",
                    )
        return _send

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
