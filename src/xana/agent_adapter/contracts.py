"""Agent adapter contracts.

Adapters translate external protocols into agent requests and render agent events back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xana.agents.contracts import AgentEvent

AdapterHealthStatus = Literal["starting", "healthy", "degraded", "stopped", "failed"]
CommandVisibility = Literal["public", "admin", "debug"]


@dataclass(frozen=True)
class InboundMessage:
    adapter_id: str
    session_id: str
    text: str
    user_id: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    args_schema: dict[str, Any] = field(default_factory=dict)
    visibility: CommandVisibility = "public"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DraftHandle:
    adapter_id: str
    session_id: str
    draft_id: str


@dataclass(frozen=True)
class RenderedMessage:
    adapter_id: str
    session_id: str
    text: str
    draft: DraftHandle | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionBinding:
    adapter_id: str
    external_thread_id: str
    session_id: str
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterHealth:
    adapter_id: str
    status: AdapterHealthStatus
    details: dict[str, Any] = field(default_factory=dict)
