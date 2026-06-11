"""Hook contracts for turn, tool, model, and adapter lifecycle points."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xana.harness_sdk.contracts import HarnessRequest
from xana.llm_driver.contracts import ModelEvent, ModelRequest
from xana.state.contracts import RunRecord
from xana.tools.contracts import ToolInvocation, ToolResult

HookKind = Literal[
    "pre_turn",
    "post_turn",
    "pre_model_request",
    "post_model_response",
    "pre_tool_use",
    "post_tool_use",
    "pre_adapter_render",
    "post_adapter_render",
    "permission_request",
    "subagent_start",
]
HookDecision = Literal["continue", "stop", "replace", "ask", "deny"]
HookHandlerType = Literal["python", "shell", "http", "prompt"]
HookFailurePolicy = Literal["fail_closed", "fail_open", "warn", "ignore"]


@dataclass(frozen=True)
class HookMatcher:
    event: HookKind
    component_pattern: str | None = None
    tool_pattern: str | None = None
    agent_pattern: str | None = None
    path_pattern: str | None = None


@dataclass(frozen=True)
class HookHandlerRef:
    handler_type: HookHandlerType
    ref: str


@dataclass(frozen=True)
class HookSpec:
    name: str
    kind: HookKind
    description: str
    matcher: HookMatcher
    handler: HookHandlerRef
    enabled: bool = True
    priority: int = 100
    timeout_s: float | None = None
    run_in_background: bool = False
    failure_policy: HookFailurePolicy = "fail_closed"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreTurnInput:
    request: HarnessRequest
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostTurnInput:
    run: RunRecord
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreModelRequestInput:
    run: RunRecord
    request: ModelRequest
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostModelResponseInput:
    run: RunRecord
    event: ModelEvent
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreToolUseInput:
    run: RunRecord
    invocation: ToolInvocation
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostToolUseInput:
    run: RunRecord
    invocation: ToolInvocation
    result: ToolResult
    metadata: dict[str, Any] = field(default_factory=dict)


HookPayload = PreTurnInput | PostTurnInput | PreModelRequestInput | PostModelResponseInput | PreToolUseInput | PostToolUseInput | dict[str, Any]


@dataclass(frozen=True)
class HookInput:
    hook: HookSpec
    session_id: str
    run_id: str | None
    payload: HookPayload
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookResult:
    decision: HookDecision
    payload: HookPayload | None = None
    reason: str | None = None
