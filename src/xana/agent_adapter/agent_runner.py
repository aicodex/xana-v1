"""GatewayAgentRunner:AgentRunner 的实现——直接调用已注册 agent 的 ``send()`` 跑完一个子 run 并取回最终文本。

与 GatewayAgentSpawner 的区别:不走 gateway._submit,因此不发渠道通知、不建卡片、不切换前台;
调用方 await 结果。等待中的任务被取消时触发子 run 的 AbortSignal(异步模式下子 run 在后台任务里跑,
不中止就会继续跑完)。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Callable, Mapping

from xana.agents.contracts import AgentRequest
from xana.agents.interfaces import Agent
from xana.extension_api.capabilities import AgentRunner
from xana.extension_api.contracts import AgentRunOutcome
from xana.harness_sdk.contracts import AbortSignal
from xana.state.contracts import SessionRecord

logger = logging.getLogger(__name__)


class GatewayAgentRunner(AgentRunner):
    def __init__(self, *, agents: Mapping[str, Agent], session_store=None) -> None:
        self._agents = agents
        self._session_store = session_store

    async def run(
        self,
        *,
        agent_name: str,
        prompt: str,
        session_id: str,
        parent_run_id: str | None = None,
        parent_session_id: str | None = None,
        tool_ceiling: frozenset[str] | None = None,
        on_tool: Callable[[str], None] | None = None,
    ) -> AgentRunOutcome:
        agent = self._agents.get(agent_name)
        if agent is None:
            raise LookupError(f"unknown agent {agent_name!r}")
        if self._session_store is not None and await self._session_store.get(session_id) is None:
            await self._session_store.upsert(SessionRecord(
                session_id=session_id, current_agent=agent_name, parent_session_id=parent_session_id,
                parent_run_id=parent_run_id, depth=1))
        run_id = f"run-{session_id}-{uuid.uuid4().hex[:8]}"
        abort = AbortSignal()
        request = AgentRequest(
            session_id=session_id, text=prompt, source="agent_runner", target_agent=agent_name,
            metadata={"gateway_run_id": run_id, "parent_run_id": parent_run_id, "abort_signal": abort},
            tool_ceiling=tool_ceiling)
        text, status, error = "", "error", "子 agent 没有产出最终回答"
        counts: dict[str, int] = {}
        try:
            # 读到流结束(不在 final 处 break):子 run 在 final 之后还要收尾(写 RunRecord、回合结束钩子);
            # 中途被取消时 aclosing 保证生成器就地关闭,不留给 GC 在别的事件循环里收尾。
            async with contextlib.aclosing(agent.send(request)) as events:
                async for event in events:
                    he = event.harness_event
                    kind = he.kind if he is not None else event.kind
                    payload = (he.payload if he is not None else event.payload) or {}
                    if kind == "tool_started":
                        name = str(payload.get("tool_name") or "")
                        counts[name] = counts.get(name, 0) + 1
                        if on_tool is not None:
                            on_tool(name)
                    elif kind == "final" and status != "completed":
                        text, status, error = str(payload.get("text") or ""), "completed", ""
                    elif kind == "error" and status != "completed":
                        error = str(payload.get("error") or payload.get("text") or "error")
                    elif kind == "cancelled":
                        status, error = "cancelled", "子 agent 被取消"
        except asyncio.CancelledError:
            abort.trigger("parent_cancelled")   # 父任务被取消:连同后台子 run 一起中止
            raise
        return AgentRunOutcome(run_id=run_id, status=status, text=text, tool_counts=counts, error=error)
