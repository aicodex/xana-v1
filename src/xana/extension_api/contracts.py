"""src/xana/extension_api/contracts.py

Extension API data contracts. Frozen dataclasses + ExtensionData (the only
type here with behavior — a type-keyed mutable store, one per scope).

Scopes in Phase 1: thread (one per session_id) and turn (one per harness run).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeVar

from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolInvocation

T = TypeVar("T")

PromptSlot = Literal["system_head", "system", "system_tail"]
SLOT_ORDER: tuple[PromptSlot, ...] = ("system_head", "system", "system_tail")


@dataclass(frozen=True)
class PromptFragment:
    """A system-prompt fragment contributed by a ContextContributor."""

    slot: PromptSlot
    content: str
    priority: int = 100  # lower sorts first within a slot
    source: str = ""


@dataclass(frozen=True)
class ContextFragment:
    """Per-turn context contributed by a TurnInputContributor."""

    content: str
    source: str = ""


@dataclass(frozen=True)
class TurnInputContext:
    session_id: str
    agent_name: str
    run_id: str
    user_input: str
    cwd: str | None = None


class ExtensionData:
    """Type-keyed mutable store. One instance per scope (thread / turn)."""

    def __init__(self, scope_id: str) -> None:
        self.scope_id = scope_id
        self._entries: dict[type, Any] = {}

    def get(self, typ: type[T]) -> T | None:
        return self._entries.get(typ)

    def get_or_init(self, typ: type[T], init: Callable[[], T]) -> T:
        if typ not in self._entries:
            self._entries[typ] = init()
        return self._entries[typ]

    def insert(self, value: T) -> T | None:
        prev = self._entries.get(type(value))
        self._entries[type(value)] = value
        return prev

    def remove(self, typ: type[T]) -> T | None:
        return self._entries.pop(typ, None)


@dataclass(frozen=True)
class ThreadStartInput:
    session_id: str
    thread_store: ExtensionData


@dataclass(frozen=True)
class ThreadStopInput:
    session_id: str
    thread_store: ExtensionData


@dataclass(frozen=True)
class TurnStartInput:
    session_id: str
    agent_name: str
    run_id: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class TurnStopInput:
    session_id: str
    agent_name: str
    run_id: str
    status: Literal["final", "error"]
    thread_store: ExtensionData
    turn_store: ExtensionData
    # 本轮已产出的回答文本(续跑判断用,如目标评估器)
    final_text: str = ""
    # 本轮是否已因 stop 续跑而再次运行(同 CC stop_hook_active):为 True 时 hook 应避免再次拦截
    continuation_active: bool = False


@dataclass(frozen=True)
class TurnAbortInput:
    session_id: str
    agent_name: str
    run_id: str
    reason: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ToolStartInput:
    session_id: str
    run_id: str
    tool_name: str
    call_id: str
    arguments: dict[str, Any]
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ToolFinishInput:
    session_id: str
    run_id: str
    tool_name: str
    call_id: str
    ok: bool
    error: str | None
    rendered: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ApprovalReviewInput:
    invocation: ToolInvocation
    result: PermissionResult  # the non-allow result a reviewer may override
    thread_store: ExtensionData
    turn_store: ExtensionData
