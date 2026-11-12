"""State contracts for sessions, runs, checkpoints, resume, and usage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

RunStatus = Literal["running", "paused", "cancelled", "failed", "completed"]
TodoStatus = Literal["pending", "in_progress", "completed"]
TODO_STATUSES: tuple[TodoStatus, ...] = get_args(TodoStatus)
GoalStatus = Literal["active", "paused", "blocked", "complete"]


@dataclass(frozen=True)
class ProcessLogPage:
    records: tuple[dict[str, str], ...]
    start_offset: int
    next_offset: int
    has_more: bool
    complete: bool
    issue: str = ""


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    session_id: str
    objective: str
    status: GoalStatus = "active"
    completion_criterion: str = ""
    token_budget: int | None = None
    tokens_used: int = 0
    steps_used: int = 0
    elapsed_ms: int = 0
    active_since: float | None = None
    reason: str = ""
    accounted_run_id: str = ""
    accounted_tokens: int = 0
    accounted_steps: int = 0
    revision: int = 0
    completion_requested: bool = False



@dataclass(frozen=True)
class TodoItem:
    content: str
    active_form: str
    status: TodoStatus

    def __post_init__(self) -> None:
        if self.status not in TODO_STATUSES:
            raise ValueError("invalid todo status")
        if not isinstance(self.content, str) or not self.content or not isinstance(self.active_form, str) or not self.active_form:
            raise ValueError("todo content and active_form must be nonempty strings")


ExitReason = Literal[
    "user_cancel",
    "tool_kill_timeout",
    "shutdown_graceful",
    "shutdown_force",
    "network_error",
    "llm_timeout",
    "session_lost",
    "tool_error",
    "unknown",
]
CheckpointName = Literal[
    "turn_started",
    "before_llm_request",
    "after_llm_response",
    "before_tool_call",
    "after_tool_call",
    "before_compact",
    "after_compact",
    "turn_finalized",
]


@dataclass(frozen=True)
class UsageSnapshot:
    """exact_* = 最近一次模型调用(上下文占用);total_* / model_calls = 本 run 全部调用之和(消耗,预算按它算)。"""
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    exact_input_tokens: int | None = None
    exact_output_tokens: int | None = None
    exact_cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None
    total_input_tokens: int = 0         # 含缓存命中
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    model_calls: int = 0


@dataclass(frozen=True)
class Checkpoint:
    name: CheckpointName
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    current_agent: str
    cwd: Path | None = None
    project_root: Path | None = None
    model: str | None = None
    effort: str | int | None = None
    active_skills: tuple[str, ...] = ()
    parent_session_id: str | None = None
    parent_agent: str | None = None
    parent_run_id: str | None = None
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    session_id: str
    agent_name: str
    status: RunStatus
    input_request: dict[str, Any] = field(default_factory=dict)
    last_checkpoint: Checkpoint | None = None
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    pause_reason: str | None = None
    abort_reason: str | None = None
    error: str | None = None
    exit_reason: ExitReason | None = None
    # §4.2 lineage:spawn 链的父 run(顶级 run 为 None)。
    parent_run_id: str | None = None
    # §4.4 任务面板展示名(gateway inflight label 同款,用户输入前 40 字符)。
    label: str = ""
    # §4.3 预算护栏:上限字典,如 {"max_tokens": N}。空 dict = 无上限。
    budget: dict = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


RESUMABLE_EXIT_REASONS: frozenset[ExitReason] = frozenset({
    "user_cancel", "tool_kill_timeout",
    "shutdown_graceful", "shutdown_force",
})
EXCLUDED_EXIT_REASONS: frozenset[ExitReason] = frozenset({
    "network_error", "llm_timeout",
    "session_lost", "tool_error", "unknown",
})
