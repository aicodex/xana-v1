"""Parallel tool-call batching (contiguous concurrency='parallel' calls batched, up to MAX_PARALLEL_TOOLS).

- Progress updates stream immediately as tools execute.
- Done events are yielded strictly in model-emit order to preserve prompt-cache stability.
- Closing the async generator cancels all unfinished tasks immediately.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from xana.protocol.diag import DiagLogger
from xana.tools.contracts import ToolActivity

MAX_PARALLEL_TOOLS = 10
_DIAG = DiagLogger("tools")

Dispatch = Callable[[dict[str, Any], Callable[[str | ToolActivity], None]], Awaitable[tuple[Any, str]]]


def partition(parallel: Sequence[bool]) -> list[list[int]]:
    """Partition call indices into contiguous parallel batches and singleton serialized batches."""
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
    update: str | ToolActivity


@dataclass(frozen=True)
class Done:
    index: int
    result: Any
    rendered: str
    elapsed_ms: int


async def run_batch(
    calls: Sequence[dict[str, Any]],
    dispatch: Dispatch,
    *,
    limit: int = MAX_PARALLEL_TOOLS,
) -> AsyncIterator[Progress | Done]:
    """Execute a batch concurrently; stream Progress immediately and Done in original order."""
    batch_t0 = time.monotonic()
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
        _DIAG.perf(
            "batch.done",
            latency_ms=(time.monotonic() - batch_t0) * 1000.0,
            slow_ms=10_000.0,
            size=len(calls),
            limit=limit,
        )
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
