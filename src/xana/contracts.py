"""Shared contract primitives.

Read this when defining module inputs and outputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class Status(str, Enum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultEnvelope:
    status: Status
    payload: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None


ComponentKind = Literal["runtime", "agent", "tool", "skill", "adapter", "llm", "memory", "config"]


@dataclass(frozen=True)
class ComponentId:
    kind: ComponentKind
    name: str
