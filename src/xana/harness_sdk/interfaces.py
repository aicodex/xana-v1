"""Harness SDK abstract interfaces.

These interfaces define the turn-loop boundary without implementing it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest, ProgressEvent
from xana.llm_driver.contracts import ModelEvent, ModelMessage, ModelRequest
from xana.skills.contracts import SkillContribution
from xana.state.contracts import RunRecord, RunStatus, UsageSnapshot
from xana.tools.contracts import ToolInvocation, ToolResult


class ProgressSink(ABC):
    @abstractmethod
    async def report(self, event: ProgressEvent) -> None: ...


class PromptAssembler(ABC):
    """Build provider-ready messages from request, context, skills, and memory."""

    @abstractmethod
    async def assemble(
        self,
        request: HarnessRequest,
        context: HarnessContext,
        skill_contributions: Sequence[SkillContribution],
    ) -> tuple[ModelMessage, ...]: ...


class ModelRequestBuilder(ABC):
    """Translate harness context and messages into an LLM driver request."""

    @abstractmethod
    async def build(
        self,
        request: HarnessRequest,
        context: HarnessContext,
        messages: tuple[ModelMessage, ...],
    ) -> ModelRequest: ...


class UsageAccumulator(ABC):
    """Merge estimates and provider-reported usage into the run usage snapshot."""

    @abstractmethod
    async def estimate_before_request(
        self,
        request: HarnessRequest,
        context: HarnessContext,
        messages: tuple[ModelMessage, ...],
    ) -> UsageSnapshot: ...

    @abstractmethod
    async def apply_model_event(self, current: UsageSnapshot, event: ModelEvent) -> UsageSnapshot: ...


class TurnController(ABC):
    """Own turn status transitions. State authority is state.RunStore/RunRecord."""

    @abstractmethod
    async def start(self, request: HarnessRequest) -> RunRecord: ...

    @abstractmethod
    async def checkpoint(self, run_id: str, name: str, payload: dict) -> None: ...

    @abstractmethod
    async def pause(self, run_id: str, reason: str) -> None: ...

    @abstractmethod
    async def resume(self, run_id: str) -> None: ...

    @abstractmethod
    async def cancel(self, run_id: str, reason: str) -> None: ...

    @abstractmethod
    async def finish(self, run_id: str, usage: UsageSnapshot) -> None: ...

    @abstractmethod
    async def fail(self, run_id: str, error: str) -> None: ...

    @abstractmethod
    async def status(self, run_id: str) -> RunStatus: ...


class ToolCallParser(ABC):
    """Extract tool invocations from model events."""

    @abstractmethod
    async def parse(self, event: ModelEvent, run_id: str, session_id: str) -> tuple[ToolInvocation, ...]: ...


class ToolDispatcher(ABC):
    """Validate, authorize, execute, and normalize tool calls."""

    @abstractmethod
    async def dispatch(self, invocation: ToolInvocation, context: HarnessContext) -> ToolResult: ...


class HarnessEventMapper(ABC):
    """Convert model/tool/run signals into public harness events."""

    @abstractmethod
    async def from_model_event(self, run: RunRecord, event: ModelEvent) -> HarnessEvent: ...

    @abstractmethod
    async def from_tool_result(self, run: RunRecord, invocation: ToolInvocation, result: ToolResult) -> HarnessEvent: ...


class Harness(ABC):
    """Top-level harness interface. Implementations yield ordered run events."""

    @abstractmethod
    def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]: ...
