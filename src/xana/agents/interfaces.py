"""Agent abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from xana.agents.contracts import AgentControlRequest, AgentEvent, AgentRequest, AgentSpec, SubagentRequest, SubagentResult
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest


class AgentRequestMapper(ABC):
    @abstractmethod
    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest: ...

    @abstractmethod
    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext: ...


class AgentEventMapper(ABC):
    @abstractmethod
    async def map(self, session_id: str, event: HarnessEvent) -> AgentEvent: ...


class SubagentDispatcher(ABC):
    @abstractmethod
    async def dispatch(self, request: SubagentRequest) -> SubagentResult: ...


class Agent(ABC):
    @abstractmethod
    def spec(self) -> AgentSpec: ...

    @abstractmethod
    def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    def control(self, request: AgentControlRequest) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    async def cancel(self, session_id: str, run_id: str | None = None) -> None: ...


class AgentRegistry(ABC):
    @abstractmethod
    async def register(self, agent: Agent) -> None: ...

    @abstractmethod
    async def get(self, name: str) -> Agent | None: ...

    @abstractmethod
    async def list_specs(self) -> tuple[AgentSpec, ...]: ...
