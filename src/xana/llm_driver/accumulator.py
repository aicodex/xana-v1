"""Linear-time tool-call assembly and per-attempt stream accumulator.

Adopted from Hermes-Agent & Kimi Code best practices:
- ToolCallAccumulator buffers argument fragments in a list[str] (O(n) join on materialize),
  handles reused index=0 parallel calls, resent full tool names, ID sanitization,
  and preserves malformed JSON errors as `input_error` (never silently turning into `{}`).
- AttemptAccumulator tracks visible vs. invisible deltas so mid-stream recovery can
  safely retry streams that died after only whitespace/thinking deltas.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from xana.llm_driver.contracts import ModelEvent, ModelUsage

_SAFE_CALL_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_CALL_ID_LEN = 64


def sanitize_tool_call_id(raw_id: Any, fallback_index: int = 0) -> str:
    """Normalize tool_call_id to [A-Za-z0-9_-], max 64 chars."""
    text = str(raw_id or "").strip()
    if not text:
        return f"call_{fallback_index}"
    cleaned = _SAFE_CALL_ID_RE.sub("_", text)[:_MAX_CALL_ID_LEN]
    return cleaned or f"call_{fallback_index}"


def _json_looks_closed(text: str) -> bool:
    s = text.strip()
    if not s or not s.startswith("{"):
        return False
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False


@dataclass
class _PendingToolCall:
    call_id: str
    name: str
    args_parts: list[str] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict)

    def raw_args(self) -> str:
        return "".join(self.args_parts)


class ToolCallAccumulator:
    """Assembles streamed tool_call deltas across provider quirks in linear time."""

    def __init__(self) -> None:
        self._by_index: dict[int, list[_PendingToolCall]] = {}
        self._order: list[_PendingToolCall] = []

    def feed(
        self,
        *,
        index: int,
        call_id: Any = None,
        name: str = "",
        args_chunk: str = "",
        provider_data: dict[str, Any] | None = None,
    ) -> None:
        bucket = self._by_index.setdefault(index, [])
        clean_id = sanitize_tool_call_id(call_id, len(self._order)) if call_id else ""

        if not bucket:
            entry = _PendingToolCall(
                call_id=clean_id or f"call_{index}",
                name=name or "",
            )
            bucket.append(entry)
            self._order.append(entry)
        else:
            current = bucket[-1]
            # Ollama quirk: parallel calls reuse index=0 with a new id + name after previous JSON closed
            if (
                clean_id
                and name
                and clean_id != current.call_id
                and current.name
                and _json_looks_closed(current.raw_args())
            ):
                entry = _PendingToolCall(call_id=clean_id, name=name)
                bucket.append(entry)
                self._order.append(entry)
            else:
                entry = current
                if clean_id and (not entry.call_id or entry.call_id == f"call_{index}"):
                    entry.call_id = clean_id
                if name:
                    # MiniMax/NIM quirk: some providers resend full name every chunk instead of delta
                    if not entry.name:
                        entry.name = name
                    elif entry.name != name and not name.startswith(entry.name):
                        entry.name += name
                    elif name.startswith(entry.name) and len(name) > len(entry.name):
                        entry.name = name

        if args_chunk:
            entry.args_parts.append(args_chunk)
        if provider_data:
            entry.provider_data.update(provider_data)

    def materialize(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for idx, entry in enumerate(self._order):
            raw = entry.raw_args().strip()
            call_id = sanitize_tool_call_id(entry.call_id, idx)
            payload: dict[str, Any] = {
                "tool_name": entry.name,
                "call_id": call_id,
                "input": {},
            }
            if entry.provider_data:
                payload["provider_data"] = dict(entry.provider_data)
            if not raw:
                payload["input"] = {}
            else:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        payload["input"] = parsed
                    else:
                        payload["input"] = {}
                        payload["input_error"] = f"tool arguments must be a JSON object, got {type(parsed).__name__}: {raw[:200]}"
                except json.JSONDecodeError as exc:
                    payload["input"] = {}
                    payload["input_error"] = f"invalid tool JSON ({exc.msg}): {raw[:200]}"
            results.append(payload)
        return results


class AttemptAccumulator:
    """Single-attempt message/usage/tool accumulator; reset cleanly on every retry."""

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: ModelUsage = ModelUsage()
        self.stop_reason: str = ""
        self.provider_request_id: str | None = None
        self.has_visible_output: bool = False
        self.malformed_tool_calls: int = 0
        self.partial_bytes: int = 0

    def observe(self, event: ModelEvent) -> None:
        if event.kind == "text_delta":
            delta = str(event.payload.get("text") or "")
            self.partial_bytes += len(delta.encode("utf-8", errors="ignore"))
            if event.payload.get("channel") == "thinking":
                self._thinking_parts.append(delta)
            else:
                self._text_parts.append(delta)
                if delta.strip():
                    self.has_visible_output = True
        elif event.kind == "tool_call":
            tc = dict(event.payload)
            self.tool_calls.append(tc)
            self.has_visible_output = True
            if tc.get("input_error"):
                self.malformed_tool_calls += 1
        elif event.kind == "usage" and event.usage is not None:
            self.usage = self.usage.add(event.usage)
        elif event.kind == "done":
            self.stop_reason = str(event.payload.get("stop_reason") or "")
            if event.payload.get("request_id"):
                self.provider_request_id = str(event.payload["request_id"])

    @property
    def text(self) -> str:
        return "".join(self._text_parts)

    @property
    def thinking(self) -> str:
        return "".join(self._thinking_parts)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
