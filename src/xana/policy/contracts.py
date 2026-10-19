"""Policy contracts for permission, sandbox, and approval."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PermissionMode = Literal["default", "ask", "auto", "plan", "accept_edits", "deny", "bypass"]
PermissionDecision = Literal["allow", "ask", "deny"]
SandboxMode = Literal["none", "read_only", "workspace_write", "restricted_network", "full"]


@dataclass(frozen=True)
class PermissionContext:
    mode: PermissionMode
    session_id: str
    agent_name: str | None = None
    run_id: str | None = None
    additional_working_dirs: tuple[Path, ...] = ()
    shall_avoid_prompts: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


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
