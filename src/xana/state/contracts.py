"""State contracts for sessions, runs, checkpoints, resume, and usage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RunStatus = Literal["running", "paused", "cancelled", "failed", "completed"]
ResumeAction = Literal["resume_from_checkpoint", "rerun_turn", "drop", "mark_cancelled"]
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
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    exact_input_tokens: int | None = None
    exact_output_tokens: int | None = None
    exact_cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None


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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResumeDecision:
    run_id: str
    action: ResumeAction
    reason: str
    checkpoint: Checkpoint | None = None
