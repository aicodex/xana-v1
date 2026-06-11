"""Observability abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.observability.contracts import DebugSnapshot, HealthReport, TraceEvent


class TraceSink(ABC):
    @abstractmethod
    async def emit(self, event: TraceEvent) -> None: ...


class MetricsSink(ABC):
    @abstractmethod
    async def increment(self, name: str, value: int = 1, tags: dict | None = None) -> None: ...

    @abstractmethod
    async def gauge(self, name: str, value: float, tags: dict | None = None) -> None: ...


class HealthCheck(ABC):
    @abstractmethod
    async def check(self) -> HealthReport: ...


class Inspector(ABC):
    @abstractmethod
    async def snapshot(self) -> DebugSnapshot: ...
