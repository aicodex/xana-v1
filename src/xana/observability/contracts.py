"""Observability contracts.

Observability displays and records state. It does not become a state authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TraceKind = Literal["runtime", "agent", "harness", "tool", "skill", "adapter", "llm", "memory"]
HealthStatus = Literal["ok", "degraded", "failed"]


@dataclass(frozen=True)
class TraceEvent:
    kind: TraceKind
    name: str
    ts: float
    session_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    component_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    component: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DebugSnapshot:
    title: str
    payload: dict[str, Any]
