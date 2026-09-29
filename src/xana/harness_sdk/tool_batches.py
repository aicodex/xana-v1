"""同一轮多个工具调用的并行执行(照 Claude Code:连续的并发安全调用成批并行,其余独占;默认上限 10)。

- 是否可并行由工具声明的 ``ToolSpec.concurrency == "parallel"`` 决定。
- 执行中的进度(report_progress / report_activity)边执行边转发。
- **完成事件按模型发出调用的顺序交付**:后发的调用即使先完成,也等前面的都完成再交付。
  调用方据此按原顺序写回历史(前缀缓存稳定、tool_use/tool_result 顺序与模型发出时一致)。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from xana.tools.contracts import ToolActivity

MAX_PARALLEL_TOOLS = 10

Dispatch = Callable[[dict[str, Any], Callable[[str | ToolActivity], None]], Awaitable[tuple[Any, str]]]


def partition(parallel: Sequence[bool]) -> list[list[int]]:
    """把调用下标分批:连续的可并行调用合成一批,不可并行的单独一批(保持原顺序)。"""
    batches: list[list[int]] = []
    for i, ok in enumerate(parallel):
        if ok and batches and len(batches[-1]) and parallel[batches[-1][-1]]:
            batches[-1].append(i)
        else:
            batches.append([i])
    return batches


@dataclass(frozen=True)
class Progress:
    index: int
    update: str | ToolActivity        # 一行进度(report_progress)或结构化活动(report_activity)


@dataclass(frozen=True)
class Done:
    index: int
    result: Any
    rendered: str
    elapsed_ms: int


async def run_batch(calls: Sequence[dict[str, Any]], dispatch: Dispatch, *,
                    limit: int = MAX_PARALLEL_TOOLS) -> AsyncIterator[Progress | Done]:
    """并发执行一批调用;产出进度事件(随时)与完成事件(按原顺序)。生成器被关闭时取消未完成的调用。"""
    sem = asyncio.Semaphore(max(1, limit))
    queue: asyncio.Queue[Progress] = asyncio.Queue()

    async def one(i: int) -> tuple[Any, str, int]:
        async with sem:
            t0 = time.monotonic()
            result, rendered = await dispatch(calls[i], lambda update, i=i: queue.put_nowait(Progress(i, update)))
            return result, rendered, int((time.monotonic() - t0) * 1000)

    tasks = [asyncio.create_task(one(i)) for i in range(len(calls))]
    finished: dict[int, Done] = {}
    next_out = 0
    try:
        while next_out < len(calls):
            getter = asyncio.create_task(queue.get())
            pending = {t for t in tasks if not t.done()} | {getter}
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield getter.result()
            else:
                getter.cancel()
            while not queue.empty():
                yield queue.get_nowait()
            for i, t in enumerate(tasks):
                if t.done() and i not in finished:
                    result, rendered, elapsed = t.result()
                    finished[i] = Done(i, result, rendered, elapsed)
            while next_out in finished:
                yield finished[next_out]
                next_out += 1
    finally:
        for t in tasks:
            if not t.done():  # 事件流被上游关闭:未完成的调用一并取消,不留后台执行
                t.cancel()
