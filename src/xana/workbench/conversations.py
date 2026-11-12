"""Project-scoped history queries over existing run records and their audit logs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

from xana.harness_sdk.events import RunEvents, RunSubscription
from xana.harness_sdk.turn_control import TurnCoordinator
from pathlib import Path
from xana.state.contracts import RunRecord, SessionRecord
from xana.state.file_runs import RunJsonlWriter
from xana.state.interfaces import RunStore, SessionStore
from xana.workbench.contracts import ConversationPage, ConversationTurn, SessionView


class ConversationService:
    def __init__(self, *, sessions: SessionStore, runs: RunStore,
                 transcripts: RunJsonlWriter, events: RunEvents, default_agent: str = "chat",
                 control: TurnCoordinator | None = None, workspace: Path | None = None) -> None:
        self._sessions = sessions
        self._runs = runs
        self._transcripts = transcripts
        self.events = events
        self._default_agent = default_agent
        self._control = control
        self.workspace = str(workspace) if workspace is not None else ""

    async def create(self) -> str:
        sid = f"web-{uuid4().hex}"
        await self._sessions.upsert(SessionRecord(sid, self._default_agent))
        return sid

    async def sessions(self) -> tuple[SessionView, ...]:
        runs = await self._runs.list_all()
        records = {item.session_id: item for item in await self._sessions.list()}
        by_id = {run.run_id: run for run in runs}
        groups: dict[str, list[RunRecord]] = {sid: [] for sid in records}
        for run in runs:
            groups.setdefault(run.session_id, []).append(run)
        result = []
        for sid, rows in groups.items():
            latest = max(rows, key=_order) if rows else None
            record = records.get(sid)
            parent_run = record.parent_run_id if record else (latest.parent_run_id if latest else None)
            parent = by_id.get(parent_run)
            _, live = self.events.capture(sid)
            result.append(SessionView(sid, latest.agent_name if latest else record.current_agent,
                record.parent_session_id if record else (parent.session_id if parent else None),
                parent_run, bool(live), latest.label if latest else sid,
                _order(latest)[0] if latest else 0.0))
        return tuple(sorted(result, key=lambda item: (item.updated_at, item.session_id), reverse=True))

    async def require_session(self, session_id: str) -> None:
        _validate_id(session_id)
        if (await self._sessions.get(session_id) is None
                and not await self._runs.list_by_session(session_id)):
            raise LookupError("session not found")

    async def run(self, session_id: str, run_id: str) -> RunRecord:
        _validate_id(session_id)
        _validate_id(run_id)
        record = await self._runs.get(run_id)
        if record is None or record.session_id != session_id:
            raise LookupError("run not found")
        return record

    async def page(self, session_id: str, *, before: str | None = None,
                   limit: int = 20, observation: RunSubscription | None = None) -> ConversationPage:
        await self.require_session(session_id)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cursor, live = ((observation.cursor, deepcopy(observation.live)) if observation is not None
                        else self.events.capture(session_id))
        rows = sorted(await self._runs.list_by_session(session_id), key=_order)
        if before is not None:
            positions = [index for index, row in enumerate(rows) if row.run_id == before]
            if not positions:
                raise ValueError("unknown history cursor")
            rows = rows[:positions[0]]
        has_more = len(rows) > limit
        selected = rows[-limit:]
        turns = await asyncio.gather(*(self._turn(row) for row in selected))
        committed = {item["row_id"] for turn in turns for item in turn.items}
        committed_inputs = {item["payload"].get("metadata", {}).get("input_id") for turn in turns for item in turn.items}
        pending = tuple({"id": item.id, "text": item.request.user_input, "delivery": item.delivery,
                         "source": str(item.request.metadata.get("source", "user"))}
                        for item in (self._control.pending_inputs(session_id) if self._control else ())
                        if item.id not in committed_inputs)
        for state in live.values():
            state["items"] = {key: value for key, value in state["items"].items() if key not in committed}
        return ConversationPage(session_id, cursor, tuple(turns), has_more,
                                selected[0].run_id if has_more else None, live, pending)

    async def _turn(self, run: RunRecord) -> ConversationTurn:
        issues: list[str] = []
        loaded = await asyncio.to_thread(self._transcripts.read, run.run_id, errors=issues)
        if loaded is None:
            return ConversationTurn(run, (), False, ("run transcript unavailable",))
        unique: dict[str, dict] = {}
        for row in loaded[0]:
            if row.get("session_id") != run.session_id or row.get("agent_name") != run.agent_name:
                issues.append("run transcript contains an unrelated row")
                continue
            payload = row["payload"]
            if not isinstance(payload.get("content"), str) or payload.get("role") not in {"user", "assistant", "tool", "system", "compact"}:
                issues.append("invalid conversation payload")
                continue
            payload.setdefault("metadata", {})
            unique.setdefault(row["row_id"], row)
        return ConversationTurn(run, tuple(unique.values()), not issues, tuple(issues))


def _order(run: RunRecord) -> tuple[float, str]:
    return float(run.metadata.get("started_at", 0)), run.run_id


def _validate_id(value: str) -> None:
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("invalid session ID")
