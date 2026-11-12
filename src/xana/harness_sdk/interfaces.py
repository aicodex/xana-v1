"""Harness SDK abstract interfaces.

These interfaces define the turn-loop boundary without implementing it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator, AsyncIterator, Awaitable, Callable

from xana.context.message import CompactSummary

from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest, ProgressEvent
from xana.llm_driver.contracts import ModelEvent, ModelMessage
from xana.state.contracts import UsageSnapshot


class ProgressSink(ABC):
    @abstractmethod
    async def report(self, event: ProgressEvent) -> None: ...


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


class Harness(ABC):
    @abstractmethod
    async def compact(self, context: HarnessContext, *,
                      on_progress: Callable[[str], Awaitable[None]] | None = None) -> CompactSummary | None:
        """Summarize and replace the existing conversation at an owned maintenance boundary."""
        ...

    @abstractmethod
    async def cancel(self, session_id: str, run_id: str | None = None) -> bool:
        """Request cancellation and wait for the matching owned execution to finish."""
        ...

    """Top-level harness interface. Implementations yield ordered run events."""

    @abstractmethod
    def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncGenerator[HarnessEvent, None]: ...
