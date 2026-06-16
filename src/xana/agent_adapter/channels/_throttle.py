"""DraftCoalescer: non-blocking, coalescing rate limiter for draft edits.

Ported from yuxu_backup gateway/draft.py (maybe_flush / _schedule_trailing /
close). Each adapter session buffer owns its own instance — global is the
wrong scope.

Semantics:
- flush(send) is synchronous and never blocks the caller. If the throttle
  window is open, the send fires immediately as a background task; otherwise
  at most ONE trailing task is armed that fires when the window expires.
  Because ``send`` closures read the session buffer's cumulative text at call
  time, the latest accumulated state wins and intermediate drafts are dropped.
- finalize(send) cancels any trailing task, waits for an in-flight send, then
  force-sends the final state (awaited).
- An internal lock serializes sends so two edits for the same message can
  never race out of order; exceptions in background sends are logged, not
  raised.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

_logger = logging.getLogger(__name__)


class DraftCoalescer:
    """At most one outbound edit per min_interval_s; never blocks the caller."""

    def __init__(self, *, min_interval_s: float = 0.25) -> None:
        self.min_interval_s = min_interval_s
        self._last_flush_mono: float | None = None
        self._lock = asyncio.Lock()
        self._trailing: asyncio.Task[None] | None = None
        self._inflight: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------

    def flush(self, send: Callable[[], Awaitable[None]]) -> None:
        """Send latest state now if the window is open, else arm one trailing task."""
        now = time.monotonic()
        if self._last_flush_mono is None or now - self._last_flush_mono >= self.min_interval_s:
            self._last_flush_mono = now
            self._inflight = asyncio.create_task(self._run_send(send))
        elif self._trailing is None or self._trailing.done():
            delay = self.min_interval_s - (now - self._last_flush_mono)
            self._trailing = asyncio.create_task(self._trailing_send(delay, send))

    async def finalize(self, send: Callable[[], Awaitable[None]],
                       *, wait_inflight: bool = True) -> None:
        """Cancel trailing, optionally wait for in-flight, then force-send。
        wait_inflight=False:detach/cancel 等"瞬时"场景用,不等当前 in-flight
        send 完成(让它在背景里跑完),立刻发新 patch — 用户感受更快。"""
        if self._trailing is not None and not self._trailing.done():
            self._trailing.cancel()
            try:
                await self._trailing
            except asyncio.CancelledError:
                pass
        self._trailing = None
        if wait_inflight and self._inflight is not None and not self._inflight.done():
            try:
                await self._inflight
            except asyncio.CancelledError:
                pass
            self._inflight = None
        self._last_flush_mono = time.monotonic()
        if wait_inflight:
            async with self._lock:
                try:
                    await send()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("draft coalescer: finalize send failed")
        else:
            # 不等 inflight,直接 spawn send;并发安全靠 lock。
            try:
                async with self._lock:
                    await send()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("draft coalescer: finalize send failed")

    # ------------------------------------------------------------------

    async def _trailing_send(self, delay: float, send: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._last_flush_mono = time.monotonic()
        await self._run_send(send)

    async def _run_send(self, send: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            try:
                await send()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("draft coalescer: background send failed")
