"""DaemonBridgeAdapter: gateway -> HTTP daemon bridge.

Per-session state:
  - monotonic seq counter
  - ring buffer (deque, default 5000 entries)
  - subscriber queue set (multi-client fan-out)

subscribe(sid, last_event_id) flow:
  1. If last_event_id is within the ring -> replay (last+1..tail) inline
  2. If last_event_id is older than the ring's oldest -> emit "replay_gap"
     then fall through to live tail
  3. Enter push loop: consume new events from a per-subscriber Queue
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from xana.agent_adapter.contracts import (
    AdapterHealth, InboundMessage, RenderedMessage, SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent


@dataclass
class _SessionState:
    seq: int = 0
    ring: deque = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    last_activity_ts: float = 0.0


class DaemonBridgeAdapter(Adapter):
    adapter_id = "cli"

    def __init__(
        self, ring_buffer_size: int = 5000,
        session_resolver: Any = None,
    ) -> None:
        self._ring_size = ring_buffer_size
        self._sessions: dict[str, _SessionState] = {}
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._status = "stopped"
        self._resolver = session_resolver

    # /clear hook: rotates the cli thread's session_id via the resolver.
    # Without this, /clear over the CLI is a no-op for session rotation
    # (legacy behaviour comment at commands.py:_cmd_clear).
    def rotate_session(self, external_thread_id: str = "") -> str:
        if self._resolver is None:
            return external_thread_id  # legacy no-op
        return self._resolver.rotate("cli", external_thread_id or "")

    def current_session(self, external_thread_id: str = "") -> str | None:
        if self._resolver is None:
            return None
        return self._resolver.current("cli", external_thread_id or "")

    def resolve_session(self, external_thread_id: str = "") -> str | None:
        """Returns the active sid for the given thread; mints if missing."""
        if self._resolver is None:
            return None
        return self._resolver.resolve("cli", external_thread_id or "")

    async def start(self) -> None:
        self._status = "healthy"

    async def stop(self) -> None:
        self._status = "stopped"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]

    async def bind_session(self, binding: SessionBinding) -> None:
        return None

    def receive(self):
        return self._receive()

    async def _receive(self):
        # Forever-loop — pump in serve.py iterates `async for inbound in
        # adapter.receive()` and would stop after the first yield if we
        # broke out. Multi-turn sessions need every submit to dispatch.
        while True:
            msg = await self._inbound.get()
            yield msg

    async def submit_inbound(self, session_id: str, text: str,
                             metadata: dict | None = None, *, attachments: tuple[dict, ...] = ()) -> None:
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=text,
            metadata=metadata or {},
            attachments=attachments,
        ))

    def _get_or_create(self, sid: str) -> _SessionState:
        st = self._sessions.get(sid)
        if st is None:
            st = _SessionState(ring=deque(maxlen=self._ring_size))
            self._sessions[sid] = st
        return st

    def _enqueue(self, sid: str, evt_type: str, data: dict) -> None:
        st = self._get_or_create(sid)
        st.seq += 1
        st.last_activity_ts = time.monotonic()
        item = (st.seq, evt_type, data)
        st.ring.append(item)
        dead: list[asyncio.Queue] = []
        for q in st.subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            st.subscribers.discard(q)
            # Wake the stream and close it. The client reconnects with its last
            # delivered sequence; the ring retains events discarded here.
            while not q.empty():
                q.get_nowait()
            q.put_nowait(None)

    async def render(self, event: AgentEvent) -> RenderedMessage:
        sid = event.session_id
        self._enqueue(sid, "agent_event", agent_event_to_wire(event))
        return RenderedMessage(
            adapter_id=self.adapter_id, session_id=sid,
            text=str((event.payload or {}).get("text", "")),
            metadata={"event_kind": event.kind},
        )

    def note_progress(self, payload: dict[str, Any]) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        self._enqueue(sid, "progress", dict(payload))

    async def subscribe(
        self, sid: str, *, last_event_id: int | None = None,
        queue_size: int = 1000,
    ) -> AsyncIterator[tuple[int, str, dict]]:
        st = self._get_or_create(sid)
        q: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        # Snapshot and registration happen without yielding: events arriving
        # during replay go only into q, after the snapshot's sequence boundary.
        replay = tuple(st.ring)
        boundary = st.seq
        st.subscribers.add(q)
        try:
            if replay:
                oldest_seq = replay[0][0]
                if last_event_id is not None and last_event_id < oldest_seq - 1:
                    yield (boundary, "replay_gap", {"reason": "out_of_range",
                                                   "oldest_available": oldest_seq})
                else:
                    threshold = last_event_id if last_event_id is not None else 0
                    for seq, t, d in replay:
                        if seq > threshold:
                            yield (seq, t, d)
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item
        finally:
            st.subscribers.discard(q)

    def note_subscribe(self, sid: str) -> None:
        st = self._get_or_create(sid)
        st.last_activity_ts = time.monotonic()

    def note_unsubscribe(self, sid: str) -> None:
        st = self._sessions.get(sid)
        if st is not None:
            st.last_activity_ts = time.monotonic()

    def session_status(self, sid: str) -> dict:
        st = self._get_or_create(sid)
        return {
            "state": "active" if st.subscribers else "idle",
            "subscribers": len(st.subscribers),
            "active_run_ids": [],
        }

    def sweep_idle(self, idle_unload_s: float) -> None:
        now = time.monotonic()
        dead = [sid for sid, st in self._sessions.items()
                if not st.subscribers
                and now - st.last_activity_ts > idle_unload_s]
        for sid in dead:
            self._sessions.pop(sid, None)


def agent_event_to_wire(ev: AgentEvent) -> dict:
    """AgentEvent → SSE 线格式(dict);逆操作 agent_event_from_wire,两者同处一地。"""
    out = {
        "kind": ev.kind,
        "session_id": ev.session_id,
        "payload": dict(ev.payload or {}),
    }
    if ev.harness_event is not None:
        out["harness_event"] = {
            "kind": ev.harness_event.kind,
            "session_id": ev.harness_event.session_id,
            "run_id": ev.harness_event.run_id,
            "payload": dict(ev.harness_event.payload or {}),
        }
    return out


def agent_event_from_wire(data: dict) -> AgentEvent:
    """SSE 线格式 → AgentEvent(xanac 客户端用)。"""
    he = data.get("harness_event")
    harness_event = None
    if he:
        harness_event = HarnessEvent(
            kind=he["kind"], session_id=he["session_id"],
            run_id=he["run_id"], payload=he.get("payload") or {})
    return AgentEvent(
        kind=data["kind"], session_id=data["session_id"],
        payload=data.get("payload") or {}, harness_event=harness_event,
    )
