"""唯一写 main history 的 consumer 循环 + store/mirror 透明包装。

Phase 2 sync 模式下相当于直调（consumer 立即处理），但代码路径保留，Phase 3
async 后台 run 时多个生产者通过同一个 queue 串行化 main 写入。

不抽 ABC（决策 2A）。MainWriter 是 boot 组装出的 concrete helper，对 harness
透明（harness 拿到的 store/mirror 就是 writer.store / writer.mirror）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Sequence

from xana.memory.contracts import ActiveContext, HistoryKind, HistoryRow, MemoryMessage, Placeholder
from xana.memory.interfaces import ActiveContextStore, HistoryMirror

_ROLE_TO_KIND: dict[str, HistoryKind] = {"user": "user", "assistant": "assistant", "tool": "tool", "system": "system"}


@dataclass
class _WriteOp:
    coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    fut: asyncio.Future


class _QueuedActiveContextStore(ActiveContextStore):
    def __init__(self, inner: ActiveContextStore, enqueue: Callable[[_WriteOp], Coroutine]) -> None:
        self._inner = inner
        self._enqueue = enqueue

    async def _run(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(_WriteOp(coro_factory=factory, fut=fut))
        return await fut

    def sdk(self):
        """Passthrough to inner store's ``sdk()`` if present.

        Bug C fix: harness uses this to drive auto-compact through the
        ContextSDK (same compact_lock as manual /compact). Returns None if
        the inner store does not back onto a ContextSDK.
        See docs/design/session-and-card-state.md §1.5.
        """
        getter = getattr(self._inner, "sdk", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return await self._inner.get(session_id, agent_name)

    async def snapshot(self, session_id: str, agent_name: str):
        return await self._inner.snapshot(session_id, agent_name)

    async def set(self, context: ActiveContext) -> None:
        await self._run(lambda: self._inner.set(context))

    async def append_message(
        self, session_id: str, agent_name: str, message: MemoryMessage
    ) -> ActiveContext:
        return await self._run(
            lambda: self._inner.append_message(session_id, agent_name, message)
        )

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        return await self._run(lambda: self._inner.compact(session_id, agent_name, summary))

    async def clear(self, session_id: str, agent_name: str) -> None:
        await self._run(lambda: self._inner.clear(session_id, agent_name))

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        await self._run(lambda: self._inner.append_placeholder(session_id, agent_name, placeholder))

    async def replace_placeholder(
        self, run_id: str, replace_with: tuple[MemoryMessage, ...]
    ) -> ActiveContext | None:
        return await self._run(lambda: self._inner.replace_placeholder(run_id, replace_with))

    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> ActiveContext | None:
        return await self._run(
            lambda: self._inner.replace_range(
                session_id, agent_name, start_idx, end_idx, summary_message
            )
        )


class _QueuedHistoryMirror(HistoryMirror):
    def __init__(self, inner: HistoryMirror, enqueue: Callable[[_WriteOp], Coroutine]) -> None:
        self._inner = inner
        self._enqueue = enqueue

    async def append(self, row: HistoryRow) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(_WriteOp(coro_factory=lambda: self._inner.append(row), fut=fut))
        await fut

    async def replay(self):
        return await self._inner.replay()

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(
            _WriteOp(
                coro_factory=lambda: self._inner.append_placeholder(session_id, agent_name, placeholder),
                fut=fut,
            )
        )
        await fut

    async def replace_placeholder(
        self, run_id: str, messages: tuple[MemoryMessage, ...]
    ) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(
            _WriteOp(
                coro_factory=lambda: self._inner.replace_placeholder(run_id, messages),
                fut=fut,
            )
        )
        await fut

    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(
            _WriteOp(
                coro_factory=lambda: self._inner.replace_range(
                    session_id, agent_name, start_idx, end_idx, summary_message
                ),
                fut=fut,
            )
        )
        await fut

    async def read_rows(self):
        read_rows = getattr(self._inner, "read_rows", None)
        if read_rows is None:
            return ()
        return await read_rows()


class MainWriter:
    """组装 queue + consumer + 包装好的 store/mirror。"""

    _SENTINEL: object = object()

    def __init__(
        self,
        inner_store: ActiveContextStore,
        inner_mirror: HistoryMirror,
    ) -> None:
        self._inner_store = inner_store
        self._inner_mirror = inner_mirror
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._broken = False
        self.store: ActiveContextStore = _QueuedActiveContextStore(inner_store, self._enqueue)
        self.mirror: HistoryMirror = _QueuedHistoryMirror(inner_mirror, self._enqueue)

    async def _enqueue(self, op: _WriteOp) -> None:
        if self._broken:
            raise RuntimeError("MainWriter broken")
        await self._queue.put(op)

    async def enqueue_replace_placeholder(
        self,
        *,
        session_id: str,
        agent_name: str,
        run_id: str,
        replace_with: Sequence[MemoryMessage],
    ) -> None:
        msgs = tuple(replace_with)
        # Enqueue store.replace_placeholder via the wrapped store (same queue).
        await self.store.replace_placeholder(run_id=run_id, replace_with=msgs)
        # Then enqueue one mirror.append per message, preserving order.
        for msg in msgs:
            row = HistoryRow(
                row_id=uuid.uuid4().hex[:8],
                session_id=session_id,
                agent_name=agent_name,
                kind=_ROLE_TO_KIND.get(msg.role, "assistant"),
                payload=msg,
                ts=time.time(),
                metadata={"run_id": run_id},
            )
            await self.mirror.append(row)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._consume(), name="xana-main-writer")

    async def _consume(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is self._SENTINEL:
                    return
                assert isinstance(item, _WriteOp)
                try:
                    result = await item.coro_factory()
                    if not item.fut.done():
                        item.fut.set_result(result)
                except Exception as exc:
                    # Per-op isolation: only this caller's future fails.
                    if not item.fut.done():
                        item.fut.set_exception(exc)
        except BaseException as exc:
            # Outer loop crashed (cancel / unexpected). Mark broken and
            # drain any pending ops so their callers do not hang.
            self._broken = True
            try:
                while True:
                    pending = self._queue.get_nowait()
                    if isinstance(pending, _WriteOp) and not pending.fut.done():
                        pending.fut.set_exception(
                            RuntimeError("MainWriter broken: consumer crashed")
                        )
            except asyncio.QueueEmpty:
                pass
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        await self._queue.put(self._SENTINEL)
        await self._task
        self._task = None
