"""Throttle: enforce min_interval_s between async actions.

Used by channel adapters to rate-limit live message-edit calls. Each
adapter chat owns its own Throttle instance — global is the wrong scope.
"""
from __future__ import annotations

import asyncio
import time


class Throttle:
    def __init__(self, *, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._last_ts: float | None = None

    async def wait(self) -> None:
        now = time.monotonic()
        if self._last_ts is not None:
            elapsed = now - self._last_ts
            remaining = self.min_interval_s - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_ts = time.monotonic()
