"""Card ViewModel:UnifiedCardState + 生命周期。

设计思路(MVVM):
- Model = `UnifiedCardState` (纯数据,平台无关)
- ViewModel = `Card` (本文件,生命周期 + 改动触发刷新 + 守门)
- View = `unified_card_feishu.render` / `unified_card_telegram.render` (平台特定)

Card 是 adapter 不再 "知道" 卡何时刷新 / 是否冻结 / 怎样合并的入口:
    card.dispatch(event)         # 事件分发,内部自动 render+deliver
    card.reveal_tools()           # 按钮回调
    card.freeze()                 # detach 瞬间
    card.terminalize(**kw)        # ph_replaced
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
    apply_detach,
    apply_ph_replaced,
    compute_top_buttons,
    load_pending_tools,
)
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent, ProgressEvent


Deliver = Callable[["Card"], Awaitable[None]]
TaskSink = Callable[[asyncio.Task[Any]], None]


# 解冻事件(detached 卡只允许这些过):
_THAW_KINDS = frozenset({"ph_replaced", "run_completed_bg"})
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

    @property
    def is_detached(self) -> bool:
        return self.state.detached and not self.terminal

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def dispatch(self, event: Any) -> asyncio.Task[Any] | None:
        """收到一条事件:守门 → apply → 重算按钮 → schedule。

        终态事件 (final/error/cancelled/ph_replaced/run_completed_bg/
        run_detached) 返回 spawn 出的 finalize task,adapter 可 await 以等
        delivery 完成(测试与命令路径都依赖这点);其它返回 None(走 throttle)。
        """
        if not self._accepts(event):
            return None
        kind = _event_kind(event)
        apply_event(self.state, event)
        self._refresh_top_buttons()
        finalize = self._is_finalize_after(kind)
        return self._schedule(finalize=finalize)

    def reveal_tools(self) -> asyncio.Task[Any] | None:
        """加载剩余工具按钮回调。"""
        if self.terminal or self.state.answer is None:
            return None
        load_pending_tools(self.state.answer)
        return self._schedule(finalize=True)

    def freeze(self) -> asyncio.Task[Any] | None:
        """detach 瞬间:折叠骨架,冻结后续刷新直到 terminalize。"""
        if self.terminal:
            return None
        apply_detach(self.state)
        self.state.top_buttons = ()
        return self._schedule(finalize=True)

    def terminalize(
        self,
        *,
        final_answer: str = "",
        ctx_used: int | None = None,
        elapsed_s: int | None = None,
    ) -> asyncio.Task[Any] | None:
        """ph_replaced / run_completed_bg 终态:解冻 → 终态 patch。"""
        if self.terminal:
            return None
        apply_ph_replaced(
            self.state,
            final_answer=final_answer,
            ctx_used=(
                ctx_used if ctx_used is not None else self.state.footer.ctx_used
            ),
            elapsed_s=(
                elapsed_s if elapsed_s is not None
                else self.state.footer.elapsed_s
            ),
        )
        self.terminal = True
        self.state.top_buttons = ()
        return self._schedule(finalize=True)

    # ------------------------------------------------------------------
    # 内部:守门 / 渲染调度
    # ------------------------------------------------------------------

    def _accepts(self, event: Any) -> bool:
        if self.terminal:
            return False
        if self.is_detached:
            # 冻结后只放解冻事件通过。
            return _event_kind(event) in _THAW_KINDS
        return True

    def _is_finalize_after(self, kind: str) -> bool:
        # ph_replaced / run_completed_bg → terminal
        if kind in _THAW_KINDS:
            self.terminal = True
            self.state.top_buttons = ()
            return True
        # harness final/error/cancelled → terminal
        if kind in _FINALIZE_HARNESS_KINDS:
            self.terminal = True
            self.state.top_buttons = ()
            return True
        # run_detached:一次 finalize patch 后进入 frozen
        if kind == "run_detached":
            return True
        return False

    def _refresh_top_buttons(self) -> None:
        if self.terminal or self.state.detached:
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
