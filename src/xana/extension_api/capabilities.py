"""src/xana/extension_api/capabilities.py

Runtime Service capability ABCs. Services are harness/runtime-provided and
constructor-injected into extensions (Rule 5: no global singletons).
Phase 1 defines the interfaces only; implementations come with consumers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from xana.extension_api.contracts import AgentRunOutcome


class SchedulerService(ABC):
    @abstractmethod
    async def add(self, *, cron: str, prompt: str, recurring: bool = False) -> str: ...

    @abstractmethod
    async def remove(self, task_id: str) -> None: ...

    @abstractmethod
    async def list_all(self) -> tuple[dict[str, Any], ...]: ...


class AgentSpawner(ABC):
    @abstractmethod
    async def spawn(
        self,
        *,
        agent_name: str,
        prompt: str,
        session_id: str,
        parent_run_id: str | None = None,
        fork: bool = False,
    ) -> str:
        """Spawn a new agent run attached to session_id (the caller-provided session).

        session_id is the existing session the spawned run joins; returns the
        new run_id assigned to the spawned run. ``parent_run_id`` records
        lineage on the child's RunRecord/SessionRecord (§4.2); ``fork=True``
        copies the parent's context prefix into the child session before the
        run starts (read-only share — the child only appends a private suffix).
        """


class AgentRunner(ABC):
    """同步运行一个子 agent 直到结束并取回最终文本(AgentSpawner 是发射后不管)。

    用于主 agent 派出子 agent 并等待结果的场景(如 deep_research 的并行子研究员)。
    实现不得触碰渠道(不发卡片、不切换前台);取消等待中的任务即中止子 run。
    """

    @abstractmethod
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
        """在 ``session_id``(调用方给的独立会话)里运行 ``agent_name``,返回最终文本。
        同一 ``session_id`` 再次运行即在原子会话上下文里继续。``tool_ceiling`` 限定子 agent 可用工具的上限
        (与它自身的工具选择取交集;None = 不设上限)。``on_tool`` 在子 agent 每次调用工具时以工具名回调。"""
