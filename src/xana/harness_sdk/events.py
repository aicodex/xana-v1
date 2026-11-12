"""Process-local execution observations; never owns workers or durable history."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from xana.harness_sdk.contracts import HarnessEvent
from xana.memory.contracts import HistoryRow


@dataclass(frozen=True)
class RunEvent:
    cursor: str
    event: HarnessEvent


@dataclass
class _Channel:
    epoch: str = field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    retained: deque[RunEvent] = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    live: dict[str, dict] = field(default_factory=dict)

    @property
    def cursor(self) -> str:
        return f"{self.epoch}:{self.sequence}"


class RunSubscription:
    def __init__(self, channel: _Channel, *, after: str | None, capacity: int) -> None:
        self._channel = channel
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(capacity)
        self.cursor = channel.cursor
        self.live = deepcopy(channel.live)
        self.reset = True
        self.replay: tuple[RunEvent, ...] = ()
        if after is not None:
            epoch, _, sequence = after.rpartition(":")
            if epoch == channel.epoch and sequence.isdecimal():
                number = int(sequence)
                oldest = int(channel.retained[0].cursor.rsplit(":", 1)[1]) if channel.retained else channel.sequence + 1
                if oldest - 1 <= number <= channel.sequence:
                    self.reset = False
                    self.replay = tuple(deepcopy(item) for item in channel.retained
                                        if int(item.cursor.rsplit(":", 1)[1]) > number)
        channel.subscribers.add(self._queue)

    async def receive(self) -> RunEvent | None:
        return await self._queue.get()

    def close(self) -> None:
        self._channel.subscribers.discard(self._queue)


class RunEvents:
    """Bounded replay plus current streamed items, shared by every run observer."""

    def __init__(self, *, retain: int = 1024, subscriber_capacity: int = 256,
                 max_idle_sessions: int = 128) -> None:
        if min(retain, subscriber_capacity, max_idle_sessions) < 1:
            raise ValueError("event capacities must be positive")
        self._retain = retain
        self._capacity = subscriber_capacity
        self._max_idle = max_idle_sessions
        self._channels: dict[str, _Channel] = {}

    def subscribe(self, session_id: str, *, after: str | None = None) -> RunSubscription:
        return RunSubscription(self._channel(session_id), after=after, capacity=self._capacity)

    def capture(self, session_id: str) -> tuple[str, dict[str, dict]]:
        channel = self._channel(session_id)
        return channel.cursor, deepcopy(channel.live)

    def committed(self, run_id: str, row: HistoryRow) -> None:
        self.publish(HarnessEvent("row_committed", row.session_id, run_id, asdict(row)))

    def publish(self, event: HarnessEvent) -> None:
        captured = deepcopy(event)
        channel = self._channel(event.session_id)
        self._project(channel, captured)
        channel.sequence += 1
        record = RunEvent(channel.cursor, captured)
        channel.retained.append(record)
        while len(channel.retained) > self._retain:
            channel.retained.popleft()
        for queue in tuple(channel.subscribers):
            try:
                queue.put_nowait(deepcopy(record))
            except asyncio.QueueFull:
                channel.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)  # Client reconnects from its last cursor.

    def _channel(self, session_id: str) -> _Channel:
        channel = self._channels.get(session_id)
        if channel is None:
            idle = [sid for sid, value in self._channels.items() if not value.live and not value.subscribers]
            for sid in idle[:max(0, len(idle) - self._max_idle + 1)]:
                del self._channels[sid]
            channel = _Channel()
            self._channels[session_id] = channel
        return channel

    @staticmethod
    def _project(channel: _Channel, event: HarnessEvent) -> None:
        if event.kind == "run_closed":
            channel.live.pop(event.run_id, None)
            return
        if event.kind == "turn_started":
            channel.live[event.run_id] = {"agent_name": event.payload.get("agent_name"), "items": {}}
        state = channel.live.get(event.run_id)
        if state is None:
            return
        if event.kind == "model_delta":
            ident = event.payload["item_id"]
            item = state["items"].setdefault(ident, {"row_id": ident, "text": "", "thinking": ""})
            field = "thinking" if event.payload.get("channel") == "thinking" else "text"
            item[field] += str(event.payload.get("text", ""))
        elif event.kind == "row_committed":
            state["items"].pop(event.payload["row_id"], None)
