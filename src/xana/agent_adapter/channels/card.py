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


# 解冻事件(detached 卡终态/退出冻结):
_THAW_KINDS = frozenset({"ph_replaced", "run_completed_bg"})
# 即使 detached 也仍要渲染的事件(compact 段挂到 PH 卡上,用户要看进度):
_DETACHED_RENDER_KINDS = frozenset({
    "compact_started", "compact_progress", "compact_finalized",
})
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
        """收到一条事件 — MVVM 数据绑定语义:

        1) state 总是被 apply(数据保真,后台 run 仍在累积答案/工具),除非已 terminal
        2) 是否渲染由"detach + 事件 kind"决定:
           - terminal 卡:丢弃
           - detached 卡:只渲 compact_* (用户要看 compact 段) 与 thaw kinds
             (ph_replaced/run_completed_bg);其它仅累积,不刷视图
           - 否则:正常渲染
        3) 终态事件返回 finalize task,adapter await 后立即落地;其它返回 None(throttle)。
        """
        if self.terminal:
            return None
        kind = _event_kind(event)
        # 用 apply 前的 detached 判断,避免漏掉"detach 事件本身"的渲染。
        was_detached = self.state.detached
        # 守门:终态拒绝(上面已挡)、detached 拒绝那些不能跨冻结的事件 — 但只是不让
        # apply_event 跑也不行,因为后台 run 的 model_delta/tool_finished 必须累积,
        # 否则 ph_replaced 终态卡显示空回答。所以 apply 永远跑,渲染另判。
        apply_event(self.state, event)
        # 是否要渲染:
        #  - apply 前不是 detached → 总渲(包括 run_detached 自身)
        #  - apply 前已 detached → 只允许 compact_* 和 thaw 解冻
        should_render = (
            not was_detached
            or kind in _THAW_KINDS
            or kind in _DETACHED_RENDER_KINDS
        )
        if not should_render:
            return None
        self._refresh_top_buttons()
        finalize = self._is_finalize_after(kind)
        return self._schedule(finalize=finalize)

    def reveal_tools(self) -> asyncio.Task[Any] | None:
        """加载剩余工具按钮回调。终态卡也允许 reveal(用户在已完成任务的卡片上
        点'显示剩余 N 工具结果',应当展开。)"""
        if self.state.answer is None:
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

    def _is_finalize_after(self, kind: str) -> bool:
        # ph_replaced / run_completed_bg → terminal(state.footer.state 已由
        # apply_ph_replaced 设为 terminal)
        if kind in _THAW_KINDS:
            self.terminal = True
            self.state.top_buttons = ()
            return True
        # harness final/error/cancelled → terminal。
        # 仅在有 answer section 时把 footer 切到 "terminal"(让 🟢 绿点出现);
        # 菜单卡(answer is None,state.menu 已被 _apply_agent_only 设为 menu 段)
        # 保持 footer.state="menu",否则会被覆盖成 terminal,跳出 🟢 + 回答用时。
        if kind in _FINALIZE_HARNESS_KINDS:
            self.terminal = True
            self.state.top_buttons = ()
            if self.state.answer is not None:
                self.state.footer.state = "terminal"
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
