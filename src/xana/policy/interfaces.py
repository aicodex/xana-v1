"""Policy abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.policy.contracts import (
    ApprovalRequest,
    ApprovalResult,
    PermissionContext,
    PermissionRequest,
    PermissionResult,
    SandboxPolicySpec,
)


class PermissionPolicy(ABC):
    @abstractmethod
    async def decide(self, request: PermissionRequest) -> PermissionResult: ...


class ApprovalProvider(ABC):
    @abstractmethod
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult: ...


class SandboxPlanner(ABC):
    @abstractmethod
    async def plan(self, context: PermissionContext, requested: SandboxPolicySpec) -> SandboxPolicySpec: ...


class SandboxRunner(ABC):
    @abstractmethod
    async def enter(self, spec: SandboxPolicySpec) -> None: ...

    @abstractmethod
    async def exit(self, spec: SandboxPolicySpec) -> None: ...


class PolicyRegistry(ABC):
    @abstractmethod
    async def permission_policy(self, name: str) -> PermissionPolicy | None: ...

    @abstractmethod
    async def approval_provider(self) -> ApprovalProvider | None: ...

    @abstractmethod
    async def sandbox_planner(self) -> SandboxPlanner | None: ...
