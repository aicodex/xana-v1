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
        # Claim the next slot before sleeping so concurrent waiters serialize
        # instead of all reading the same stale timestamp and firing together.
        now = time.monotonic()
        if self._last_ts is None or now - self._last_ts >= self.min_interval_s:
            self._last_ts = now
            return
        slot = self._last_ts + self.min_interval_s
        self._last_ts = slot
        await asyncio.sleep(slot - now)
