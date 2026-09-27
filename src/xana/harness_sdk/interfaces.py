"""Harness SDK abstract interfaces.

These interfaces define the turn-loop boundary without implementing it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

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
    """Top-level harness interface. Implementations yield ordered run events."""

    @abstractmethod
    def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]: ...
