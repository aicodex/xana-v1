"""CliAdapter: stdin/stdout adapter for xanac.

Pattern: inbound messages are queued via submit(); each call to receive()
yields exactly one queued message. render() writes the event payload to
the output stream and signals turn_complete when kind=="final" or "error".

Tool events render as a compact two-line block matching cc's
AssistantToolUseMessage (components/messages/AssistantToolUseMessage.tsx:182-228):

    ● Tool(preview)
      first line of result (truncated to 120 chars with …)

preview comes from the model whenever possible:

  1. ``input["description"]`` — the short human-readable label the model is
     prompted to attach to every tool call (cc + xana Bash schema both
     have this field);
  2. for ``Bash``, the ``# label`` comment on the command's first line
     (cc's commentLabel.ts), then the truncated command itself;
  3. tool-specific main field (Read/Write/Edit: file_path; Glob/Grep: pattern;
     memory_search: query);
  4. fallback to a flat kv preview.

Failed calls append ``— FAILED`` to the header. This trims walls of Bash /
find output into a glanceable summary while keeping the model's prose in
its own streaming channel.
"""
from __future__ import annotations

import asyncio
import sys
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
            self._render_tool_event(event, text)
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
