"""Deterministic test doubles for Stage 2: FakeExecutionEnvironment, InMemoryPolicyState, RecordingPolicyEventSink."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from xana.policy.contracts import (
    PermissionMode,
    ProcessOutcome,
    ProcessSpec,
    SessionPolicySnapshot,
)
from xana.policy.interfaces import AbortLike, ExecutionEnvironment, PolicyEventSink, PolicyStatePort
from xana.protocol.primitives import ComponentVersion


class InMemoryPolicyState(PolicyStatePort):
    """Standalone in-memory PolicyStatePort (zero host dependencies)."""

    def __init__(self, initial: dict[str, SessionPolicySnapshot] | None = None) -> None:
        self._sessions: dict[str, SessionPolicySnapshot] = dict(initial or {})

    def set_session(self, snapshot: SessionPolicySnapshot) -> None:
        self._sessions[snapshot.session_id] = snapshot

    async def get_session_policy(self, session_id: str) -> SessionPolicySnapshot:
        return self._sessions.get(session_id, SessionPolicySnapshot(session_id=session_id))

    async def save_permission_mode(self, session_id: str, mode: PermissionMode) -> None:
        cur = await self.get_session_policy(session_id)
        self._sessions[session_id] = replace(cur, permission_mode=mode)

    async def add_permission_grant(self, session_id: str, fingerprint: str) -> None:
        cur = await self.get_session_policy(session_id)
        grants = tuple(sorted(set(cur.permission_grants) | {fingerprint}))
        self._sessions[session_id] = replace(cur, permission_grants=grants)


class RecordingPolicyEventSink(PolicyEventSink):
    """Records published policy events for standalone tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def publish(self, kind: str, session_id: str, run_id: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, session_id, run_id, dict(payload)))


class FakeExecutionEnvironment(ExecutionEnvironment):
    """Deterministic scripted ExecutionEnvironment for offline & second-host tests."""

    def __init__(
        self,
        outcomes: list[ProcessOutcome] | None = None,
        *,
        handler: Callable[[ProcessSpec], ProcessOutcome] | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._handler = handler
        self.specs: list[ProcessSpec] = []
        self.cancelled_ids: list[str] = []

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(name="fake_exec_env", kind="exec_env", version="1.0.0", policy_version="1.0.0")

    async def run_process(
        self,
        spec: ProcessSpec,
        *,
        abort: AbortLike | None = None,
    ) -> ProcessOutcome:
        self.specs.append(spec)
        if abort is not None and abort.aborted:
            return ProcessOutcome(exit_code=-15, stdout="", stderr="aborted", cancelled=True)
        if self._handler is not None:
            return self._handler(spec)
        if self._outcomes:
            return self._outcomes.pop(0)
        return ProcessOutcome(exit_code=0, stdout="", stderr="")

    async def cancel(self, exec_id: str) -> None:
        self.cancelled_ids.append(exec_id)
