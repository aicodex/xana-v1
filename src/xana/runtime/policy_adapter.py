"""Host adapters connecting SessionConfigStore/SessionStore/RunEvents to xana.policy ports."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from xana.harness_sdk.contracts import HarnessEvent
from xana.harness_sdk.events import RunEvents
from xana.policy.contracts import PermissionMode, SessionPolicySnapshot
from xana.policy.interfaces import PolicyEventSink, PolicyStatePort
from xana.session_config.interfaces import SessionConfigStore
from xana.state.interfaces import SessionStore


class RuntimePolicyStateAdapter(PolicyStatePort):
    """Bridges SessionConfigStore + SessionStore to PolicyStatePort."""

    def __init__(self, *, configs: SessionConfigStore, sessions: SessionStore | None = None) -> None:
        self._configs = configs
        self._sessions = sessions

    async def get_session_policy(self, session_id: str) -> SessionPolicySnapshot:
        cfg = await self._configs.get(session_id)
        record = await self._sessions.get(session_id) if self._sessions is not None else None
        return SessionPolicySnapshot(
            session_id=session_id,
            permission_mode=cfg.permission_mode,
            plan_mode=cfg.metadata.get("mode") == "plan",
            permission_grants=tuple(cfg.permission_grants),
            parent_session_id=record.parent_session_id if record else None,
        )

    async def save_permission_mode(self, session_id: str, mode: PermissionMode) -> None:
        cfg = await self._configs.get(session_id)
        await self._configs.save(replace(cfg, permission_mode=mode))

    async def add_permission_grant(self, session_id: str, fingerprint: str) -> None:
        cfg = await self._configs.get(session_id)
        grants = tuple(sorted(set(cfg.permission_grants) | {fingerprint}))
        await self._configs.save(replace(cfg, permission_grants=grants))


class RuntimePolicyEventSink(PolicyEventSink):
    """Bridges RunEvents to PolicyEventSink."""

    def __init__(self, events: RunEvents) -> None:
        self._events = events

    def publish(self, kind: str, session_id: str, run_id: str, payload: dict[str, Any]) -> None:
        self._events.publish(HarnessEvent(kind, session_id, run_id, payload))  # type: ignore[arg-type]
