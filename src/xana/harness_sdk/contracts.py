"""Harness SDK contracts.

The harness owns turn lifecycle and event stream shape. Run state is owned by state.RunRecord.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

from xana.policy.contracts import PermissionContext
from xana.state.contracts import UsageSnapshot

if TYPE_CHECKING:
    from xana.hooks.interfaces import HookRunner
    from xana.llm_driver.interfaces import ModelRegistry
    from xana.memory.interfaces import (
        ActiveContextStore,
        HistoryMirror,
        MemoryInjectorRegistry,
        MemorySearchEngine,
        MemoryTree,
    )
    from xana.observability.interfaces import TraceSink
    from xana.skills.interfaces import SkillRegistry
    from xana.state.interfaces import StateRepository
    from xana.tools.interfaces import ToolRegistry

HarnessEventKind = Literal["turn_started", "model_delta", "tool_started", "tool_finished", "paused", "cancelled", "final", "error"]


@dataclass(frozen=True)
class AbortSignal:
    aborted: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ProgressEvent:
    session_id: str
    run_id: str | None
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessRequest:
    session_id: str
    agent_name: str
    user_input: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessContext:
    session_id: str
    agent_name: str
    cwd: str
    model: str
    effort: str | int | None = None
    run_id: str | None = None
    permission: PermissionContext | None = None
    state_repository: "StateRepository | None" = None
    active_context_store: "ActiveContextStore | None" = None
    history_mirror: "HistoryMirror | None" = None
    memory_tree: "MemoryTree | None" = None
    memory_search_engine: "MemorySearchEngine | None" = None
    memory_injector_registry: "MemoryInjectorRegistry | None" = None
    tool_registry: "ToolRegistry | None" = None
    skill_registry: "SkillRegistry | None" = None
    model_registry: "ModelRegistry | None" = None
    hook_runner: "HookRunner | None" = None
    trace_sink: "TraceSink | None" = None
    progress_sink: "ProgressSink | None" = None
    abort: AbortSignal = field(default_factory=AbortSignal)
    async_mode: bool = False
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessEvent:
    kind: HarnessEventKind
    session_id: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    usage: UsageSnapshot | None = None
