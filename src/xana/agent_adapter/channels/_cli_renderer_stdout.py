"""StdoutRenderer: dual-view CLI surface for CliRenderer.

Spec §2.1/§2.2: when a run detaches, surface a 🟡 marker and stop
streaming further chunks for that run; when it later finishes in
background, print a ✅ marker plus the rendered draft; if the user
switches back (gateway emits `attached`, mapped to on_foreground_changed
by CliAdapter), print a ➡ marker and resume streaming.

Streaming behaviour for the active run is unchanged: incremental draft
chunks are written verbatim. Tool/final/error frames stay with
CliAdapter.render() to keep the yuxu-style footer card + cc tool blocks
intact and avoid double-print.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from xana.agent_adapter.interfaces import CliRenderer
from xana.agents.contracts import AgentEvent

if TYPE_CHECKING:
    from xana.agent_adapter.channels._draft import TurnDraft


def _render_draft(draft: "TurnDraft | None") -> str:
    """Pretty-print a TurnDraft to plain text for stdout.

    Keeps it minimal: thinking (if any) → content → tool block headers.
    Mirrors the footer-less, terminal-friendly shape used by oneshot
    xanac — no Markdown rendering, no card chrome.
    """
    if draft is None:
        return ""
    parts: list[str] = []
    thinking = getattr(draft, "thinking", "") or ""
    if thinking.strip():
        title = getattr(draft, "thinking_title", None) or "💭 思考过程"
        parts.append(f"{title}\n{thinking.rstrip()}")
    content = getattr(draft, "content", "") or ""
    if content.strip():
        parts.append(content.rstrip())
    for blk in getattr(draft, "tool_blocks", None) or []:
        try:
            parts.append(blk.header())
        except Exception:
            pass
    return "\n\n".join(parts)


class StdoutRenderer(CliRenderer):
    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        # Runs that have detached → suppress further on_event streaming
        # until they re-attach (on_foreground_changed).
        self._detached: set[str] = set()
        self._labels: dict[str, str] = {}

    @property
    def out(self) -> TextIO:
        # Resolve sys.stdout lazily so capsys redirection works.
        if self._out is sys.stdout:
            return sys.stdout
        return self._out

    def _id(self, run_id: str) -> str:
        label = self._labels.get(run_id)
        if label and label != run_id:
            return f"{run_id} · {label}"
        return run_id

    async def on_run_started(self, run_id: str, label: str) -> None:
        if label:
            self._labels[run_id] = label
        # A re-used run_id that comes back as a fresh start should stream again.
        self._detached.discard(run_id)

    async def on_event(self, run_id: str, event: AgentEvent) -> None:
        # Suppress chunks for runs that have been moved to the background;
        # the eventual ph_replaced / run_completed_bg call will surface the
        # finished draft in one shot.
        if run_id in self._detached:
            return
        if event.kind != "draft":
            return
        payload = event.payload or {}
        text = payload.get("text") or payload.get("delta") or ""
        if not text:
            return
        self.out.write(str(text))
        self.out.flush()

    async def on_run_detached(self, run_id: str) -> None:
        self._detached.add(run_id)
        self.out.write(f"\n🟡 [{self._id(run_id)}] 已转入后台\n")
        self.out.flush()

    async def on_foreground_changed(self, run_id: str) -> None:
        # Gateway emits "attached" — CliAdapter maps it to this method.
        # Resume streaming for the re-attached run.
        self._detached.discard(run_id)
        self.out.write(f"\n➡ [{self._id(run_id)}] 已切回前台\n")
        self.out.flush()

    async def on_ph_replaced(self, run_id: str, draft: "TurnDraft | None") -> None:
        body = _render_draft(draft)
        if body:
            self.out.write(f"\n✅ [{self._id(run_id)}] 后台已完成:\n{body}\n")
        else:
            # Foreground finalize path (no detach happened): preserve the
            # original behaviour of just terminating the stream with \n.
            self.out.write("\n")
        self.out.flush()
        self._detached.discard(run_id)

    async def on_run_completed_bg(self, run_id: str, draft: "TurnDraft | None") -> None:
        body = _render_draft(draft)
        if body:
            self.out.write(f"\n✅ [{self._id(run_id)}] 后台已完成:\n{body}\n")
            self.out.flush()
        self._detached.discard(run_id)

    async def on_compact_started(self, run_id: str, fork_point: int) -> None:
        return None

    async def on_compact_finalized(self, run_id: str, summary: str) -> None:
        return None
