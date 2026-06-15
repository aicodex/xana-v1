"""In-memory state repository for sessions and runs."""
from __future__ import annotations

from dataclasses import replace

from xana.state.contracts import (
    EXCLUDED_EXIT_REASONS,
    RESUMABLE_EXIT_REASONS,
    Checkpoint,
    RunRecord,
    RunStatus,
    SessionRecord,
    UsageSnapshot,
)
from xana.state.interfaces import RunStore, SessionStore, StateRepository


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._store: dict[str, SessionRecord] = {}

    async def get(self, session_id: str) -> SessionRecord | None:
        return self._store.get(session_id)

    async def upsert(self, record: SessionRecord) -> None:
        self._store[record.session_id] = record

    async def patch(self, session_id: str, fields: dict) -> SessionRecord:
        current = self._store[session_id]
        updated = replace(current, **fields)
        self._store[session_id] = updated
        return updated

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self._store.values())

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class InMemoryRunStore(RunStore):
    def __init__(self) -> None:
        self._store: dict[str, RunRecord] = {}

    async def create(self, record: RunRecord) -> None:
        self._store[record.run_id] = record

    async def get(self, run_id: str) -> RunRecord | None:
        return self._store.get(run_id)

    async def update_status(self, run_id: str, status: RunStatus, fields: dict | None = None) -> RunRecord:
        current = self._store[run_id]
        extras = fields or {}
        updated = replace(current, status=status, **extras)
        self._store[run_id] = updated
        return updated

    async def checkpoint(self, run_id: str, checkpoint: Checkpoint) -> None:
        current = self._store[run_id]
        self._store[run_id] = replace(current, last_checkpoint=checkpoint)

    async def update_usage(self, run_id: str, usage: UsageSnapshot) -> None:
        current = self._store[run_id]
        self._store[run_id] = replace(current, usage=usage)

    async def list_unfinished(self) -> tuple[RunRecord, ...]:
        # P5 tightening: only records with a RESUMABLE exit_reason qualify.
        # A bare status=="running"/"paused" record with no exit_reason is an
        # orphan from a crash — startup_scan is responsible for stamping
        # shutdown_force on those; until then they MUST NOT leak into /resume.
        out: list[RunRecord] = []
        for rec in self._store.values():
            if rec.exit_reason in EXCLUDED_EXIT_REASONS:
                continue
            if rec.exit_reason in RESUMABLE_EXIT_REASONS:
                out.append(rec)
        return tuple(out)

    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]:
        return tuple(r for r in self._store.values() if r.session_id == session_id)

    async def list_all(self) -> tuple[RunRecord, ...]:
        """Return every record. Not on the RunStore ABC — used by startup_scan
        to find orphan running/paused records that lack an exit_reason."""
        return tuple(self._store.values())


class InMemoryStateRepository(StateRepository):
    def __init__(self) -> None:
        self._sessions = InMemorySessionStore()
        self._runs = InMemoryRunStore()

    def sessions(self) -> SessionStore:
        return self._sessions

    def runs(self) -> RunStore:
        return self._runs
