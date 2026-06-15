"""Graceful shutdown helper(P5.6)。

`graceful_shutdown(gateway, grace_seconds)`:
  1. 调 `gateway.cancel_all()` 翻所有 background runs 的 abort
  2. 等 ≤ grace_seconds drain
  3. 超时返回 True(下次启动按 shutdown_force 进 /resume)
     正常 drain 返回 False
"""
from __future__ import annotations

import asyncio


async def graceful_shutdown(gateway, *, grace_seconds: float = 2.0) -> bool:
    await gateway.cancel_all()
    deadline = asyncio.get_event_loop().time() + grace_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not gateway.has_background():
            return False
        await asyncio.sleep(0.02)
    return True
