"""Single-consumer queue for PH replacement + compact replace_range.

Decouples run consumers from main-context writes so concurrent runs can
finish in any order without racing the main message list. The consumer
acquires the store lock + mirrors writes; runs only ever write their own
runs/{run_id}.jsonl independently.

Spec: docs/superpowers/specs/2026-06-14-async-chat-design.md sections 1.5-1.7.

The consumer MUST be scheduled as a SEPARATE asyncio task from any run
consumer; otherwise sync mode deadlocks because the run consumer awaits
_invoke, _invoke enqueues, and no drainer is running. See spec section 11.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from xana.memory.contracts import MemoryMessage
from xana.memory.interfaces import ActiveContextStore, HistoryMirror


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplaceItem:
    """Enqueue when a run finalizes — replace its PH in main with run history."""

    run_id: str
    session_id: str
    agent_name: str
    messages: tuple[MemoryMessage, ...]


@dataclass(frozen=True)
class ReplaceRangeItem:
    """Enqueue when compact finalizes — collapse [start_idx, end_idx) into summary."""

    session_id: str
    agent_name: str
    start_idx: int
    end_idx: int
    summary: MemoryMessage


class WriteQueue:
    """Thin asyncio.Queue wrapper for ReplaceItem / ReplaceRangeItem."""

    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()

    async def put(self, item: ReplaceItem | ReplaceRangeItem) -> None:
        await self._q.put(item)

    async def get(self) -> ReplaceItem | ReplaceRangeItem:
        return await self._q.get()


class WriteQueueConsumer:
    """Single consumer that drains WriteQueue and applies items to store + mirror."""

    def __init__(
        self,
        queue: WriteQueue,
        *,
        store: ActiveContextStore,
        mirror: HistoryMirror | None,
    ) -> None:
        self._q = queue
        self._store = store
        self._mirror = mirror
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            try:
                await self._apply(item)
            except Exception:
                _logger.exception("WriteQueueConsumer: apply failed for %r", item)

    async def _apply(self, item: ReplaceItem | ReplaceRangeItem) -> None:
        if isinstance(item, ReplaceItem):
            await self._store.replace_placeholder(item.run_id, item.messages)
            if self._mirror is not None:
                try:
                    await self._mirror.replace_placeholder(item.run_id, item.messages)
                except NotImplementedError:
                    pass
        elif isinstance(item, ReplaceRangeItem):
            await self._store.replace_range(
                item.session_id,
                item.agent_name,
                item.start_idx,
                item.end_idx,
                item.summary,
            )
            if self._mirror is not None:
                try:
                    await self._mirror.replace_range(
                        item.session_id,
                        item.agent_name,
                        item.start_idx,
                        item.end_idx,
                        item.summary,
                    )
                except NotImplementedError:
                    pass

    def stop(self) -> None:
        self._stopped.set()
