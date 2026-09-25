"""Agent contracts.

Agents configure harness usage and expose chat/control APIs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xana.component.contracts import ComponentSpec
from xana.harness_sdk.contracts import HarnessEvent, PromptProfile, VerifyCheck
from xana.policy.contracts import SandboxPolicySpec

AgentEventKind = Literal["draft", "tool", "final", "control_result", "error", "cancelled"]
MemoryScope = Literal["user", "project", "agent", "session"]
IsolationMode = Literal["none", "worktree", "process", "remote"]
DelegationMode = Literal["inline", "background", "isolated"]


@dataclass(frozen=True)
class AgentSpec:
    component: ComponentSpec
    prompt_id: str
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    allowed_skills: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    model: str | None = None
    effort: str | int | None = None
    permission_policy: str | None = None
    sandbox_policy: SandboxPolicySpec | None = None
    memory_scope: MemoryScope | None = None
    isolation: IsolationMode = "none"
    background: bool = False
    parent_agent: str | None = None
    max_turns: int | None = None
    # None → 完整默认 13 段系统提示;非空 → 替换全部静态段,动态段照常注入
    system_prompt: str | None = None
    # Agent 身份,追加在 INTRO 之后(仅 system_prompt=None 时生效)
    identity: str = ""
    # 会话级提示词开关;None = 交互模式默认值。会话创建时固化进 SessionConfig
    prompt_profile: PromptProfile | None = None
    # 回合结束时的检查命令(命令型验证,对应 CC 命令型 Stop hook)
    verify: tuple[VerifyCheck, ...] = ()


@dataclass(frozen=True)
class AgentRequest:
    session_id: str
    text: str
    source: str
    target_agent: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentControlRequest:
    session_id: str
    op: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentRequest:
    parent_session_id: str
    parent_agent: str
    parent_run_id: str | None
    target_agent: str
    task: str
    new_session_id: str
    mode: DelegationMode = "isolated"
    allowed_tools: tuple[str, ...] | None = None
    fork_context_messages: bool = False
    inherit_cwd: bool = True
    isolation: IsolationMode = "none"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentResult:
    parent_session_id: str
    child_session_id: str
    target_agent: str
    ok: bool
    result: str
    transcript_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DelegationPolicy:
    allow_subagents: bool = True
    max_depth: int = 1
    allowed_agents: tuple[str, ...] = ()
    permission_inheritance: Literal["inherit", "override", "intersect"] = "intersect"
    tool_inheritance: Literal["inherit", "override", "intersect"] = "intersect"
    context_fork: Literal["none", "messages", "full"] = "none"
    avoid_prompts_for_background: bool = True
    bubble_prompts_to_parent: bool = False


@dataclass(frozen=True)
class AgentEvent:
    kind: AgentEventKind
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    harness_event: HarnessEvent | None = None
