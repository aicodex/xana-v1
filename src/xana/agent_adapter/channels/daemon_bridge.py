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


@dataclass
class _SessionState:
    seq: int = 0
    ring: deque = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    last_activity_ts: float = 0.0


class DaemonBridgeAdapter(Adapter):
    adapter_id = "cli"

    def __init__(self, ring_buffer_size: int = 5000) -> None:
        self._ring_size = ring_buffer_size
        self._sessions: dict[str, _SessionState] = {}
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._status = "stopped"

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
        msg = await self._inbound.get()
        yield msg

    async def submit_inbound(self, session_id: str, text: str,
                             metadata: dict | None = None) -> None:
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id,
            session_id=session_id,
            text=text,
            metadata=metadata or {},
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

    async def render(self, event: AgentEvent) -> RenderedMessage:
        sid = event.session_id
        self._enqueue(sid, "agent_event", _agent_event_to_dict(event))
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
        # Replay phase
        if st.ring:
            oldest_seq = st.ring[0][0]
            if last_event_id is not None and last_event_id < oldest_seq - 1:
                yield (st.seq, "replay_gap", {"reason": "out_of_range",
                                              "oldest_available": oldest_seq})
            else:
                threshold = last_event_id if last_event_id is not None else 0
                for seq, t, d in list(st.ring):
                    if seq > threshold:
                        yield (seq, t, d)
        st.subscribers.add(q)
        try:
            while True:
                item = await q.get()
                yield item
        finally:
            st.subscribers.discard(q)


def _agent_event_to_dict(ev: AgentEvent) -> dict:
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
