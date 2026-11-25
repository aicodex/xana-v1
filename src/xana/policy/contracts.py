"""Policy and execution-environment frozen data contracts (schema v1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

POLICY_SCHEMA_VERSION: int = 1

PermissionMode = Literal["manual", "yolo", "auto"]
PermissionDecision = Literal["allow", "ask", "deny"]
ApprovalDecision = Literal["approved", "rejected", "cancelled"]
SandboxMode = Literal["none", "read_only", "workspace_write", "restricted_network", "full"]
ShellRisk = Literal["routine", "dangerous", "unknown"]
PathOperation = Literal["read", "write"]


@dataclass(frozen=True)
class PermissionContext:
    mode: PermissionMode
    session_id: str
    agent_name: str | None = None
    run_id: str | None = None
    additional_working_dirs: tuple[Path, ...] = ()
    shall_avoid_prompts: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    plan_mode: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"manual", "yolo", "auto"}:
            raise ValueError("invalid permission mode")


@dataclass(frozen=True)
class SandboxPolicySpec:
    mode: SandboxMode
    cwd: Path
    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    allow_network: bool = False
    allow_subprocess: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str | None = None
    updated_context: PermissionContext | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    session_id: str
    owner_session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    cwd: str
    created_at: float
    status: Literal["pending", "approved", "rejected", "cancelled"] = "pending"


@dataclass(frozen=True)
class RiskAssessment:
    """Structured output of a CommandRiskClassifier."""

    risk: ShellRisk
    reason: str = ""
    matched_rule: str = ""


@dataclass(frozen=True)
class SessionPolicySnapshot:
    """Narrow view of session policy state needed by PermissionService."""

    session_id: str
    permission_mode: PermissionMode | None = None
    plan_mode: bool = False
    permission_grants: tuple[str, ...] = ()
    parent_session_id: str | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    """Host-agnostic tool authorization request (decouples policy from tools)."""

    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    cwd: str
    read_only: bool
    permission: PermissionContext
    agent_name: str | None = None


@dataclass(frozen=True)
class ProcessSpec:
    """Declarative specification for a subprocess execution inside an ExecutionEnvironment."""

    cwd: str
    shell: str | None = None
    argv: tuple[str, ...] | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 120.0
    max_output_chars: int = 50_000
    memory_limit_bytes: int | None = None
    exec_id: str = ""


@dataclass(frozen=True)
class ProcessOutcome:
    """Settled outcome of a subprocess execution."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    duration_ms: float = 0.0
