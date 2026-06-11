"""Runtime contracts.

Runtime wires modules and owns process lifecycle only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RuntimePhase = Literal["created", "configured", "restored", "running", "stopping", "stopped", "failed"]
TaskStatus = Literal["queued", "running", "cancelled", "failed", "completed"]


@dataclass(frozen=True)
class BootRequest:
    project_root: str
    config_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeHealth:
    phase: RuntimePhase
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRequest:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    name: str


@dataclass(frozen=True)
class TaskState:
    task_id: str
    status: TaskStatus
    details: dict[str, Any] = field(default_factory=dict)
