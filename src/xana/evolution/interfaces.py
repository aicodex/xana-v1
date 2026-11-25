"""Abstract interfaces for the Evolution & Evaluation subsystem (Stage 7)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from xana.evolution.contracts import (
    LedgerEntry,
    LintViolation,
    PromotionCriteria,
    PromotionDecision,
)
from xana.protocol.primitives import ComponentVersion


class IterationLedgerStore(ABC):
    """Append-only iteration ledger contract (no update or delete)."""

    @abstractmethod
    async def append(self, entry: LedgerEntry) -> None: ...

    @abstractmethod
    async def read_all(self) -> tuple[LedgerEntry, ...]: ...

    @abstractmethod
    async def recent(self, n: int) -> tuple[LedgerEntry, ...]: ...

    @abstractmethod
    def lint(self) -> list[LintViolation]: ...


class PromotionGate(ABC):
    """Evolvable anti-overfitting promotion / rollback decision gate."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def evaluate(
        self,
        base_per_case: list[dict[str, Any]],
        cand_per_case: list[dict[str, Any]],
        *,
        criteria: PromotionCriteria,
    ) -> PromotionDecision: ...


class BenchmarkRunner(ABC):
    """Benchmark suite execution & scoring contract."""

    @abstractmethod
    def suite(self, name: str) -> Any: ...

    @abstractmethod
    async def run(
        self,
        suite: Any,
        cases: tuple[Any, ...],
        *,
        repeat: int = 1,
        agent: str | None = None,
        label: str = "",
        tape: tuple[str, ...] = (),
        on_case: Callable[[str], None] | None = None,
        reuse: bool = True,
    ) -> str: ...

    @abstractmethod
    async def score(self, suite: Any, batch: str, *, max_age_s: float = 21600.0) -> dict[str, Any]: ...

    @abstractmethod
    def batch_runs(self, suite: Any, batch: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def reusable_rows(self, suite: Any, agent: str) -> dict[str, list[dict[str, Any]]]: ...

    @abstractmethod
    def heldout_run_ids(self) -> frozenset[str]: ...
