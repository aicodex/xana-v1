"""StdoutRenderer: one-shot CLI surface for CliRenderer.

Writes per-event payload text to sys.stdout (or any TextIO) for the
oneshot xanac flow. Lifecycle methods are silent — oneshot doesn't show
run identity, can't switch foreground, and never sees a compact UX.
on_ph_replaced terminates the stream with a single newline.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from xana.agent_adapter.interfaces import CliRenderer
from xana.agents.contracts import AgentEvent

if TYPE_CHECKING:
    from xana.agent_adapter.channels._draft import TurnDraft


class StdoutRenderer(CliRenderer):
    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout

    @property
    def out(self) -> TextIO:
        # Resolve sys.stdout lazily so capsys redirection works.
        if self._out is sys.stdout:
            return sys.stdout
        return self._out

    async def on_run_started(self, run_id: str, label: str) -> None:
        return None

    async def on_event(self, run_id: str, event: AgentEvent) -> None:
        # Stream only incremental draft chunks. Final/tool/error frames are
        # cumulative or formatted blocks — leaving them to CliAdapter's
        # legacy render() path keeps the yuxu-style footer card and the
        # cc-style tool blocks intact, and avoids double-printing the
        # cumulative reply at turn end.
        if event.kind != "draft":
            return
        payload = event.payload or {}
        text = payload.get("text") or payload.get("delta") or ""
        if not text:
            return
        self.out.write(str(text))
        self.out.flush()

    async def on_run_detached(self, run_id: str) -> None:
        return None

    async def on_foreground_changed(self, run_id: str) -> None:
        return None

    async def on_ph_replaced(self, run_id: str, draft: "TurnDraft | None") -> None:
        self.out.write("\n")
        self.out.flush()

    async def on_run_completed_bg(self, run_id: str, draft: "TurnDraft | None") -> None:
        return None

    async def on_compact_started(self, run_id: str, fork_point: int) -> None:
        return None

    async def on_compact_finalized(self, run_id: str, summary: str) -> None:
        return None
