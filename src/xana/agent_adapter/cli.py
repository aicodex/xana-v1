"""CliAdapter: stdin/stdout adapter for xanac.

Pattern: inbound messages are queued via submit(); each call to receive()
yields exactly one queued message. render() writes the event payload to
the output stream and signals turn_complete when kind=="final" or "error".
"""
from __future__ import annotations

import asyncio
import sys
from typing import AsyncGenerator, AsyncIterator, TextIO

from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


class CliAdapter(Adapter):
    def __init__(
        self,
        *,
        out: TextIO | None = None,
        adapter_id: str = "cli",
        session_id: str = "cli-session",
    ) -> None:
        self.out = out if out is not None else sys.stdout
        self.adapter_id = adapter_id
        self.session_id = session_id
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.turn_complete: asyncio.Event = asyncio.Event()
        self._status: str = "stopped"

    async def start(self) -> None:
        self._status = "healthy"

    async def stop(self) -> None:
        self._status = "stopped"

    async def submit(self, text: str) -> None:
        self.turn_complete.clear()
        await self._inbound.put(
            InboundMessage(
                adapter_id=self.adapter_id,
                session_id=self.session_id,
                text=text,
            )
        )

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive()

    async def _receive(self) -> AsyncGenerator[InboundMessage, None]:
        msg = await self._inbound.get()
        yield msg

    async def render(self, event: AgentEvent) -> RenderedMessage:
        text = str(event.payload.get("text", ""))
        if event.kind == "draft":
            # Streaming chunk — write as-is, no trailing newline.
            if text:
                self.out.write(text)
                self.out.flush()
        elif event.kind == "tool":
            # Tool finished — render the block on its own line.
            if text:
                self.out.write(text)
                if not text.endswith("\n"):
                    self.out.write("\n")
                self.out.flush()
        elif event.kind == "final":
            # Turn end — only emit a terminating newline so streamed deltas
            # aren't repeated. payload["text"] is the cumulative reply, already
            # shown via the draft stream.
            self.out.write("\n")
            self.out.flush()
            self.turn_complete.set()
        elif event.kind == "error":
            if text:
                self.out.write(text)
                if not text.endswith("\n"):
                    self.out.write("\n")
                self.out.flush()
            self.turn_complete.set()
        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=text,
            metadata={"event_kind": event.kind},
        )

    async def bind_session(self, binding: SessionBinding) -> None:
        return None

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]
