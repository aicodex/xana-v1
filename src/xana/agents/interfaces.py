"""Agent abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator

from xana.agents.contracts import AgentControlRequest, AgentEvent, AgentRequest, AgentSpec
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest


class AgentRequestMapper(ABC):
    @abstractmethod
    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest: ...

    @abstractmethod
    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext: ...


class AgentEventMapper(ABC):
    @abstractmethod
    async def map(self, session_id: str, event: HarnessEvent) -> AgentEvent: ...


class Agent(ABC):
    @abstractmethod
    def spec(self) -> AgentSpec: ...

    @abstractmethod
    def send(self, request: AgentRequest) -> AsyncGenerator[AgentEvent, None]: ...

    @abstractmethod
    def control(self, request: AgentControlRequest) -> AsyncGenerator[AgentEvent, None]: ...

    @abstractmethod
    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        """Stop and join the matching execution; gateway controls may request stop without joining."""
        ...


class AgentRegistry(ABC):
    @abstractmethod
    async def register(self, agent: Agent) -> None: ...

    @abstractmethod
    async def get(self, name: str) -> Agent | None: ...

    @abstractmethod
    async def list_specs(self) -> tuple[AgentSpec, ...]: ...

    @abstractmethod
    async def refresh(self, *, force: bool = False) -> tuple[Exception, ...]:
        """与定义来源(如磁盘上的 AGENT.md)同步:新增 / 变更的重新加载,已删除的移除;返回新出现的加载错误。"""

    @abstractmethod
    async def remove(self, name: str) -> Path:
        """删除一个可删除的 agent 定义并立即生效,返回被删文件;不可删除或不存在时抛 LookupError。"""

    @abstractmethod
    async def set_allow_exec(self, name: str, allowed: bool, reason: str = "") -> Path:
        """用户开启 / 关闭某个文件 agent 的 Bash 权限(写进其 AGENT.md 的目标契约)并立即生效,返回该文件;
        内置或不存在的 agent 抛 LookupError。只供用户命令调用,不暴露给模型。"""
