"""Browser projections; source execution and conversation state remain authoritative."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xana.state.contracts import RunRecord


@dataclass(frozen=True)
class SessionView:
    session_id: str
    agent_name: str
    parent_session_id: str | None
    parent_run_id: str | None
    running: bool
    title: str
    updated_at: float


@dataclass(frozen=True)
class ConversationTurn:
    run: RunRecord
    items: tuple[dict[str, Any], ...]
    history_complete: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationPage:
    session_id: str
    cursor: str
    turns: tuple[ConversationTurn, ...]
    has_more: bool
    next_before: str | None
    live: dict[str, dict] = field(default_factory=dict)
    pending_inputs: tuple[dict[str, str], ...] = ()
