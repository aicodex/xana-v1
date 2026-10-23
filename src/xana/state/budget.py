"""跨 run 预算护栏 BudgetTracker(§4.3)—— boot 组装的 concrete helper,不抽 ABC。

职责:
  - 在 harness update_usage 写点累计 per-session / per-day token(跨 run)
  - RunRecord.budget 有上限的 run 每轮 check;超限返回类型化原因字符串,
    由 harness 翻译成"收尾信号"(RunRecord.metadata["budget_exceeded"] +
    progress_sink 事件),触发优雅落地而非裸死。

不做阶段四的完整 episode 状态机(软/硬阈值、landing/closing 两阶段)。
"""
from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING, Callable

from xana.state.contracts import UsageSnapshot

if TYPE_CHECKING:
    from xana.state.interfaces import RunStore


def _tokens(usage: UsageSnapshot) -> int:
    """本 run 消耗 token:全部模型调用之和;provider 没报用量时退回 estimated。"""
    total = usage.total_input_tokens + usage.total_output_tokens
    if total:
        return total
    return (usage.estimated_input_tokens or 0) + (usage.estimated_output_tokens or 0)


class BudgetTracker:
    """per-session / per-day 累计 + per-run / session / day 上限检查。

    budget 字典键(全部可选):
      max_tokens           —— 单 run 上限(每轮检查,超限触发收尾)
      max_session_tokens   —— 单 session 累计上限(跨 run)
      max_day_tokens       —— 当日累计上限(跨 run、跨 session)
    构造参数 session_limit / day_limit 是 budget 缺省时的全局兜底。
    """

    def __init__(
        self,
        *,
        session_limit: int | None = None,
        day_limit: int | None = None,
        clock: Callable[[], float] = _time.time,
    ) -> None:
        self._session_limit = session_limit
        self._day_limit = day_limit
        self._clock = clock
        self._session_totals: dict[str, int] = {}
        self._day_totals: dict[str, int] = {}

    def record(self, session_id: str, usage: UsageSnapshot) -> None:
        """harness update_usage 写点调用:把一次 run 的用量计入累计。"""
        n = _tokens(usage)
        self._session_totals[session_id] = self._session_totals.get(session_id, 0) + n
        day = self._day_key()
        self._day_totals[day] = self._day_totals.get(day, 0) + n

    def session_total(self, session_id: str) -> int:
        return self._session_totals.get(session_id, 0)

    def day_total(self) -> int:
        return self._day_totals.get(self._day_key(), 0)

    def check(self, *, session_id: str, budget: dict, usage: UsageSnapshot) -> str | None:
        """返回类型化超限原因,未超限返回 None。调用侧不累计(累计只在 record)。"""
        tokens = _tokens(usage)
        max_tokens = budget.get("max_tokens")
        if isinstance(max_tokens, (int, float)) and tokens > max_tokens:
            return "run_budget_exceeded"
        session_cap = budget.get("max_session_tokens", self._session_limit)
        if isinstance(session_cap, (int, float)) and self.session_total(session_id) + tokens > session_cap:
            return "session_budget_exceeded"
        day_cap = budget.get("max_day_tokens", self._day_limit)
        if isinstance(day_cap, (int, float)) and self.day_total() + tokens > day_cap:
            return "day_budget_exceeded"
        return None

    def _day_key(self) -> str:
        return _time.strftime("%Y-%m-%d", _time.localtime(self._clock()))


async def day_usage(runs: "RunStore", day: str | None = None) -> int:
    """某天(本地日期 YYYY-MM-DD,缺省今天)开始的全部 run 的 token 消耗之和,按持久化的 RunRecord 计算
    (跨进程、重启不丢;BudgetTracker 的内存累计只覆盖本进程)。"""
    day = day or _time.strftime("%Y-%m-%d")
    total = 0
    for rec in await runs.list_all():
        started = rec.metadata.get("started_at")
        if isinstance(started, (int, float)) and _time.strftime("%Y-%m-%d", _time.localtime(started)) == day:
            total += _tokens(rec.usage)
    return total
