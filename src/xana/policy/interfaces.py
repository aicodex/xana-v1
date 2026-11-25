"""Abstract interfaces for execution environment, path boundaries, risk classification, and policy ports."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from xana.policy.contracts import (
    PathOperation,
    PermissionMode,
    PermissionResult,
    ProcessOutcome,
    ProcessSpec,
    RiskAssessment,
    SessionPolicySnapshot,
)
from xana.protocol.primitives import ComponentVersion


class AbortLike(Protocol):
    """Structural abort signal satisfied by harness_sdk.AbortSignal or test doubles."""

    @property
    def aborted(self) -> bool: ...

    async def wait(self) -> None: ...


class CommandRiskClassifier(ABC):
    """Evolvable shell command risk classifier (RSI strategy slot)."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def classify(self, command: str) -> RiskAssessment: ...


class PathBoundaryPolicy(ABC):
    """Path access boundary check (protected directories / workspace confinement)."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def check_path(self, path: Path, *, operation: PathOperation, cwd: Path) -> PermissionResult: ...


class ExecutionEnvironment(ABC):
    """Unified subprocess execution boundary with timeout & process-group isolation."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    async def run_process(
        self,
        spec: ProcessSpec,
        *,
        abort: AbortLike | None = None,
    ) -> ProcessOutcome: ...

    @abstractmethod
    async def cancel(self, exec_id: str) -> None: ...


class PolicyStatePort(ABC):
    """Host persistence port for session permission mode, grants, and ancestry."""

    @abstractmethod
    async def get_session_policy(self, session_id: str) -> SessionPolicySnapshot: ...

    @abstractmethod
    async def save_permission_mode(self, session_id: str, mode: PermissionMode) -> None: ...

    @abstractmethod
    async def add_permission_grant(self, session_id: str, fingerprint: str) -> None: ...


class PolicyEventSink(ABC):
    """Host event publication port for approval & permission lifecycle events."""

    @abstractmethod
    def publish(self, kind: str, session_id: str, run_id: str, payload: dict[str, Any]) -> None: ...
