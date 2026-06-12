"""CliAdapter: stdin/stdout adapter for xanac.

Pattern: inbound messages are queued via submit(); each call to receive()
yields exactly one queued message. render() writes the event payload to
the output stream and signals turn_complete when kind=="final" or "error".

Tool events render as a compact two-line block matching cc's
AssistantToolUseMessage (components/messages/AssistantToolUseMessage.tsx:182-228):

    ● Tool(preview)
      first line of result (truncated to 120 chars with …)

When the turn finalizes, an append-only footer card is emitted in yuxu's
yuxuc shape (services/gateway/adapters/cli.py:138-142):

    ――――――――――――――――――――
    _model: deepseek-v4-flash · elapsed: 12.3s · tokens: 234 in / 567 out_

Footer is suppressed when the run produced no usage telemetry (the test
events emitted directly into render() carry no harness_event), so the
"no double-print" contract for bare AgentEvents stays intact. We don't
attempt live in-place redraw (cc does it via Ink/React, not raw ANSI);
oneshot mode appends once on final, an interactive REPL — when it lands
— can layer a live redraw on top.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, AsyncGenerator, AsyncIterator, TextIO

from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


from xana.agent_adapter._tool_titles import (
    TOOL_GLYPH as _TOOL_GLYPH,
    _PREVIEW_LIMIT,
    _RESULT_PREVIEW_LIMIT,
    _ELLIPSIS,
    bash_comment_label as _bash_comment_label,
    first_line_preview as _first_line_preview,
    tool_header as _tool_header,
    tool_preview as _tool_preview,
    truncate as _truncate,
)
_FOOTER_DIVIDER = "―" * 20  # U+2015 × 20, matches yuxuc


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
        self._turn_start_ts: float | None = None
        self._draft_seen_this_turn: bool = False
        self.last_progress: dict[str, Any] | None = None
        self._progress_callback = None

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
        if self._turn_start_ts is None:
            self._turn_start_ts = time.monotonic()
            self._draft_seen_this_turn = False
        text = str(event.payload.get("text", ""))
        if event.kind == "draft":
            # Streaming chunk — write as-is, no trailing newline.
            if text:
                self.out.write(text)
                self.out.flush()
            self._draft_seen_this_turn = True
        elif event.kind == "tool":
            self._render_tool_event(event, text)
        elif event.kind == "final":
            # Turn end — terminating newline, then a yuxu-style footer card
            # if telemetry is present. payload["text"] is the cumulative reply,
            # already shown via the draft stream — do NOT re-emit it.
            # Exception: command results emit only a final event without drafts,
            # so write the text if no drafts were seen this turn.
            if not self._draft_seen_this_turn and text:
                self.out.write(text)
                if not text.endswith("\n"):
                    self.out.write("\n")
            else:
                self.out.write("\n")
            self._render_footer_card(event)
            self.out.flush()
            self.turn_complete.set()
            self._turn_start_ts = None
        elif event.kind == "error":
            if text:
                self.out.write(text)
                if not text.endswith("\n"):
                    self.out.write("\n")
                self.out.flush()
            self.turn_complete.set()
            self._turn_start_ts = None
        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=text,
            metadata={"event_kind": event.kind},
        )

    def _render_footer_card(self, event: AgentEvent) -> None:
        """Append the yuxu-style finalize card: divider + italic meta line.

        Suppressed when there's no telemetry to report (e.g. unit-test events
        constructed directly, no harness_event). This keeps the bare-final
        path output identical to the pre-card behavior.
        """
        meta_pairs = self._collect_footer_meta(event)
        if not meta_pairs:
            return
        meta_line = " · ".join(f"{k}: {v}" for k, v in meta_pairs)
        self.out.write(_FOOTER_DIVIDER + "\n")
        self.out.write(f"_{meta_line}_\n")

    def _collect_footer_meta(self, event: AgentEvent) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        usage = getattr(event.harness_event, "usage", None) if event.harness_event else None
        if usage is not None:
            if usage.model:
                pairs.append(("model", str(usage.model)))
            tin = usage.exact_input_tokens
            tout = usage.exact_output_tokens
            if tin is not None or tout is not None:
                pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
        elif self.last_progress is not None:
            tin = self.last_progress.get("tokens_in")
            tout = self.last_progress.get("tokens_out")
            if tin is not None or tout is not None:
                pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
        if pairs and self.last_progress is not None:
            elapsed = float(self.last_progress.get("elapsed_s", 0.0))
            pairs.append(("elapsed", f"{elapsed:.1f}s"))
        elif self._turn_start_ts is not None and pairs:
            import time as _t
            elapsed = max(0.0, _t.monotonic() - self._turn_start_ts)
            pairs.append(("elapsed", f"{elapsed:.1f}s"))
        return pairs

    def _render_tool_event(self, event: AgentEvent, text: str) -> None:
        """Two-line compact form: `● Tool(preview)` then `  first_result_line`.

        Empty preview → header just shows `● Tool`. Empty result → header
        only. Failed tools append `— FAILED` to the header."""
        tool_name = str(event.payload.get("tool_name") or "?")
        ok = event.payload.get("ok", True)
        header = _tool_header(tool_name, event.payload.get("input") or {}, ok=bool(ok))
        self.out.write("\n" + header + "\n")
        first = _first_line_preview(text)
        if first:
            self.out.write(f"  {first}\n")
        self.out.flush()

    def note_progress(self, payload: dict[str, Any]) -> None:
        """Adapter hook for progress updates. Stores latest; REPL may register a
        callback to repaint the footer in place."""
        self.last_progress = dict(payload)
        if self._progress_callback is not None:
            self._progress_callback(payload)

    def set_progress_callback(self, cb) -> None:
        self._progress_callback = cb

    async def bind_session(self, binding: SessionBinding) -> None:
        return None

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]


