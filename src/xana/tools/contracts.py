"""Tool contracts.

Tools declare validation, permission, sandbox, execution input, and output shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from xana.component.contracts import ComponentSpec
from xana.policy.contracts import PermissionContext, SandboxPolicySpec

PermissionDecision = Literal["allow", "ask", "deny"]
ToolConcurrency = Literal["parallel", "serialized", "exclusive"]
InterruptBehavior = Literal["cancel", "block", "ignore"]

# Structured failure attribution.
#
# Superset of codex ToolItemFailureKind (`tool_error`, `approval_denied`,
# `approval_aborted`, `sandbox_denied`, `policy_forbidden`) plus the additional
# kinds CC and yuxu need to distinguish at the observability layer.
#
# Semantics — only set when `ToolResult.ok is False`:
#   validation         input failed Tool.validate or schema check
#   policy_forbidden   PermissionPolicy.decide returned "deny" (config rule)
#   approval_denied    user said deny via ApprovalProvider
#   approval_aborted   approval timed out / user aborted before deciding
#   sandbox_denied     sandbox layer refused (e.g. write outside writable_roots)
#   timeout            execution exceeded ToolSpec.timeout_s
#   execution          runtime error inside the tool body (codex tool_error)
#   cancelled          caller-initiated cancel via Tool.cancel
#   not_found          target resource (file / endpoint / record) absent
#   unsupported        capability missing (MCP server down, feature gated)
ToolFailureKind = Literal[
    "validation",
    "policy_forbidden",
    "approval_denied",
    "approval_aborted",
    "sandbox_denied",
    "timeout",
    "execution",
    "cancelled",
    "not_found",
    "unsupported",
]


@dataclass(frozen=True)
class ToolSpec:
    component: ComponentSpec
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False
    concurrency: ToolConcurrency = "serialized"
    interrupt_behavior: InterruptBehavior = "block"
    permission_policy: str | None = None
    sandbox_policy: SandboxPolicySpec | None = None
    timeout_s: float | None = None
    max_result_chars: int = 50_000
    user_facing_name: str | None = None


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    input: dict[str, Any]
    call_id: str
    session_id: str
    run_id: str | None = None


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    agent_name: str
    cwd: str
    permission: PermissionContext
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolArtifact:
    name: str
    path: Path | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDiff:
    path: Path
    diff: str


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: Any
    structured_content: dict[str, Any] | None = None
    error: str | None = None
    failure_kind: ToolFailureKind | None = None
    rendered: str | None = None
    artifacts: tuple[ToolArtifact, ...] = ()
    diffs: tuple[ToolDiff, ...] = ()
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    truncated: bool = False
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
