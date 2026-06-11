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


_TOOL_GLYPH = "●"
_PREVIEW_LIMIT = 80
_RESULT_PREVIEW_LIMIT = 120
_ELLIPSIS = "…"
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
        """Pull model/tokens/elapsed off the harness UsageSnapshot when present.

        elapsed is computed from the adapter's per-turn start timestamp so the
        figure reflects wall time the user actually waited (matches yuxu's
        format_llm_stats_footer in services/gateway/reply_helpers.py:30-48)."""
        pairs: list[tuple[str, str]] = []
        usage = getattr(event.harness_event, "usage", None) if event.harness_event else None
        if usage is not None:
            if usage.model:
                pairs.append(("model", str(usage.model)))
            tin = usage.exact_input_tokens
            tout = usage.exact_output_tokens
            if tin is not None or tout is not None:
                pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
        if self._turn_start_ts is not None and (pairs or usage is not None):
            elapsed = max(0.0, time.monotonic() - self._turn_start_ts)
            pairs.append(("elapsed", f"{elapsed:.1f}s"))
        return pairs

    def _render_tool_event(self, event: AgentEvent, text: str) -> None:
        """Two-line compact form: `● Tool(preview)` then `  first_result_line`.

        Empty preview → header just shows `● Tool`. Empty result → header
        only. Failed tools append `— FAILED` to the header."""
        tool_name = str(event.payload.get("tool_name") or "?")
        ok = event.payload.get("ok", True)
        preview = _tool_preview(event.payload.get("input") or {}, tool_name)
        header = f"{_TOOL_GLYPH} {tool_name}"
        if preview:
            header += f"({preview})"
        if ok is False:
            header += " — FAILED"
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


def _tool_preview(tool_input: dict[str, Any], tool_name: str) -> str:
    """Pick the most informative short label for a tool call.

    Priority order mirrors cc's AssistantToolUseMessage:
      1. model-supplied input["description"];
      2. Bash `# label` comment on the command's first line;
      3. tool-specific main field (file_path / pattern / query / command);
      4. flat kv preview as a last resort.
    """
    desc = tool_input.get("description")
    if isinstance(desc, str) and desc.strip():
        return _truncate(desc.strip(), _PREVIEW_LIMIT)

    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        if command:
            label = _bash_comment_label(command)
            if label:
                return _truncate(label, _PREVIEW_LIMIT)
            return _truncate(command, _PREVIEW_LIMIT)

    main_field_by_tool: dict[str, tuple[str, ...]] = {
        "Read": ("file_path", "path"),
        "Write": ("file_path", "path"),
        "Edit": ("file_path", "path"),
        "Glob": ("pattern",),
        "Grep": ("pattern",),
        "memory_search": ("query",),
    }
    for field in main_field_by_tool.get(tool_name, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return _truncate(value, _PREVIEW_LIMIT)

    if not tool_input:
        return ""
    if len(tool_input) == 1:
        only = next(iter(tool_input.values()))
        return _truncate(str(only), _PREVIEW_LIMIT)
    return _truncate(" ".join(f"{k}={v}" for k, v in tool_input.items()), _PREVIEW_LIMIT)


def _bash_comment_label(command: str) -> str | None:
    """If the first line of a bash command is a `# comment` (not a shebang),
    return its text. Mirrors cc's tools/BashTool/commentLabel.ts:8-13."""
    first_line = command.split("\n", 1)[0].strip()
    if not first_line.startswith("#") or first_line.startswith("#!"):
        return None
    label = first_line.lstrip("#").strip()
    return label or None


def _first_line_preview(text: str) -> str:
    """First non-blank stripped line of `text`, collapsed + truncated."""
    if not text:
        return ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return _truncate(stripped, _RESULT_PREVIEW_LIMIT)
    return ""


def _truncate(s: str, limit: int) -> str:
    collapsed = " ".join(s.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)] + _ELLIPSIS
