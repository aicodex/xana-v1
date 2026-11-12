"""Stop scheduler claims, finish claimed jobs, then drain conversation execution."""
from __future__ import annotations
import asyncio
from xana.agent_adapter.interfaces import AgentGateway
from xana.runtime.scheduler import SchedulerRunner

async def graceful_shutdown(gateway: AgentGateway, *, grace_seconds: float = 2.0,
                            scheduler_runner: SchedulerRunner | None = None) -> bool:
    if scheduler_runner is not None:
        scheduler_runner.stop()
        await scheduler_runner.drain()
    await gateway.cancel_all()
    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not gateway.has_active_runs():
            return False
        await asyncio.sleep(0.02)
    return gateway.has_active_runs()
