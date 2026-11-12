"""Card ViewModel:UnifiedCardState + 生命周期。

设计思路(MVVM):
- Model = `UnifiedCardState` (纯数据,平台无关)
- ViewModel = `Card` (本文件,生命周期 + 改动触发刷新 + 守门)
- View = `unified_card_feishu.render` / `unified_card_telegram.render` (平台特定)

Card 是 adapter 不再 "知道" 卡何时刷新 / 是否终态 / 怎样合并的入口:
    card.dispatch(event)         # 事件分发,内部自动 render+deliver
    card.reveal_tools()           # 按钮回调
绑定:`card.bind_transport(deliver, task_sink)` 注入平台交付 + task 追踪。

drop / dirty / coalesce / terminal 判断全部在 Card 内,adapter 只:
1) 建 Card + bind_transport
2) 把入站事件转发给 card.dispatch
3) 解析按钮 callback_data 找到 Card 调对应方法
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from xana.agent_adapter.channels._throttle import DraftCoalescer
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState,
    compute_top_buttons,
    load_pending_tools,
)
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent, ProgressEvent


Deliver = Callable[["Card"], Awaitable[None]]
TaskSink = Callable[[asyncio.Task[Any]], None]


_FINALIZE_HARNESS_KINDS = frozenset({"final", "error", "cancelled"})


def _event_kind(event: Any) -> str:
    """提取事件 kind 字符串。"""
    if isinstance(event, HarnessEvent):
        return event.kind
    if isinstance(event, AgentEvent):
        if event.harness_event is not None:
            return event.harness_event.kind
        return event.kind
    if isinstance(event, ProgressEvent):
        payload = event.payload or {}
        return str(payload.get("kind") or event.message or "")
    return ""


class Card:
    """单张卡的 ViewModel。"""

    def __init__(
        self,
        state: UnifiedCardState,
        *,
        coalesce_interval_s: float = 0.25,
    ) -> None:
        self.state = state
        self.coalescer = DraftCoalescer(min_interval_s=coalesce_interval_s)
        self.terminal = False
        self.message_id: str | None = None
        self._deliver: Deliver | None = None
        self._task_sink: TaskSink | None = None

    # ------------------------------------------------------------------
    # 绑定与查询
    # ------------------------------------------------------------------

    def bind_transport(self, *, deliver: Deliver,
                       task_sink: TaskSink | None = None) -> None:
        """注入平台交付(send/patch)和 task 追踪(可选)。"""
        self._deliver = deliver
        self._task_sink = task_sink

    @property
    def card_id(self) -> str:
        return self.state.card_id

    def dispatch(self, event: Any) -> asyncio.Task[Any] | None:
        """Apply ordered run events and flush terminal state immediately."""
        if self.terminal:
            return None
        kind = _event_kind(event)
        apply_event(self.state, event)
        self.terminal = self.state.terminal
        self._refresh_top_buttons()
        return self._schedule(finalize=kind in _FINALIZE_HARNESS_KINDS)

    def set_answer_page(self, page: int) -> asyncio.Task[Any] | None:
        """翻页:更新 answer.page,触发一次 patch。"""
        if self.state.answer is None:
            return None
        self.state.answer.page = max(0, page)
        return self._schedule(finalize=True)

    def set_expand(self, section: str) -> asyncio.Task[Any] | None:
        """切到 expand 模式 (thinking/tools/answer);其它段隐藏。"""
        if section not in ("thinking", "tools", "answer"):
            return None
        self.state.expand_mode = section  # type: ignore[assignment]
        self.state.expand_page = 0
        return self._schedule(finalize=True)

    def set_expand_page(self, page: int) -> asyncio.Task[Any] | None:
        """expand 模式下翻页。"""
        if self.state.expand_mode == "default":
            return None
        self.state.expand_page = max(0, page)
        return self._schedule(finalize=True)

    def clear_expand(self) -> asyncio.Task[Any] | None:
        """从 expand 模式返回默认视图。"""
        self.state.expand_mode = "default"
        self.state.expand_page = 0
        return self._schedule(finalize=True)

    def reveal_tools(self) -> asyncio.Task[Any] | None:
        """加载剩余工具按钮回调:数据落地 + finalize 强制 patch。
        finalize 只重置 _last_flush_mono,后续 model_delta 仍按节流刷新。"""
        if self.state.answer is None:
            return None
        load_pending_tools(self.state.answer)
        return self._schedule(finalize=True)

    def _refresh_top_buttons(self) -> None:
        if self.terminal:
            self.state.top_buttons = ()
        else:
            self.state.top_buttons = compute_top_buttons(self.state)

    def _schedule(self, *, finalize: bool) -> asyncio.Task[Any] | None:
        if self._deliver is None:
            return None
        deliver = self._deliver
        if finalize:
            async def _runner() -> None:
                await self.coalescer.finalize(lambda: deliver(self))
            try:
                task = asyncio.create_task(_runner())
            except RuntimeError:
                return None
            if self._task_sink is not None:
                self._task_sink(task)
            return task
        self.coalescer.flush(lambda: deliver(self))
        return None
