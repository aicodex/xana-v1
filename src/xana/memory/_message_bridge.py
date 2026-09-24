"""Bridge between legacy memory contracts and ContextSDK Message types.

P3-D wires the ContextSDK in as the canonical store backing
``InMemoryActiveContextStore``. Legacy callers keep using
``MemoryMessage`` / ``Placeholder``; this module is the translation
layer in both directions.

Only this module is allowed to mint ``_PlaceholderInternal`` outside of
``xana.context`` itself — every other place must go through the SDK.
"""
from __future__ import annotations

import json
from typing import Iterable
from uuid import uuid4

from xana.context._internal import _PlaceholderInternal, _ToolCallInternal
from xana.context.message import (
    AssistantMessage,
    CompactPlaceholder,
    CompactSummary,
    ContextNote,
    Message,
    RegularPlaceholder,
    ToolResultMessage,
    UserMessage,
)
from xana.memory.contracts import MemoryMessage, Placeholder


# ---------------------------------------------------------------------------
# legacy -> SDK
# ---------------------------------------------------------------------------


def memory_message_to_sdk(mm: MemoryMessage) -> Message | None:
    """Convert a legacy ``MemoryMessage`` to an SDK ``Message``.

    Returns ``None`` for roles that the SDK does not represent (currently
    ``"system"`` — the SDK treats the system content as a separate string
    passed into ``to_anthropic`` / ``to_openai``).
    """
    role = mm.role
    if role == "user" and mm.metadata.get("note_kind"):
        return ContextNote.from_legacy_metadata(mm.metadata)
    if role == "user":
        return UserMessage(text=mm.content)
    if role == "assistant":
        tool_calls = _parse_assistant_tool_calls(mm.metadata)
        return AssistantMessage(
            text=mm.content, tool_calls=tool_calls,
            reasoning=str(mm.metadata.get("reasoning_content") or ""),
        )
    if role == "tool":
        return ToolResultMessage(
            tool_use_id=str(mm.metadata.get("tool_use_id", "")),
            content=mm.content,
            ok=bool(mm.metadata.get("ok", True)),
            tool_name=str(mm.metadata.get("tool_name", "")),
            revealed_tools=tuple(mm.metadata.get("revealed_tools") or ()),
        )
    if role == "compact":
        # Legacy compact-boundary marker — make a synthetic CompactSummary
        # whose _internal carries an empty transcript path. The summary
        # text is the legacy content.
        return CompactSummary(
            summary=mm.content,
            _internal=_PlaceholderInternal(run_id="", ph_id="", transcript_path=""),
        )
    # role == "system" or unknown -> filter out at SDK level
    return None


def note_to_memory_message(note: ContextNote) -> MemoryMessage:
    """ContextNote 的 legacy 形态:content 为模型可见的包裹文本,去重/计量字段放 metadata。"""
    return MemoryMessage(role="user", content=ContextNote.wrap(note.text),
                         metadata=note.legacy_metadata())


def placeholder_to_sdk(ph: Placeholder, *, transcript_path: str = "") -> RegularPlaceholder:
    return RegularPlaceholder(
        label=ph.label,
        _internal=_PlaceholderInternal(
            run_id=ph.run_id,
            ph_id=str(uuid4()),
            transcript_path=transcript_path,
        ),
    )


def to_sdk_item(
    item: MemoryMessage | Placeholder, *, transcript_path: str = ""
) -> Message | None:
    if isinstance(item, Placeholder):
        return placeholder_to_sdk(item, transcript_path=transcript_path)
    if isinstance(item, MemoryMessage):
        return memory_message_to_sdk(item)
    return None


def _parse_assistant_tool_calls(metadata: dict) -> tuple[_ToolCallInternal, ...]:
    """Reconstruct ``_ToolCallInternal`` tuples from legacy assistant
    metadata.

    Legacy producer (DefaultHarness `_run_query_loop`, around line 903)
    stores each tool call as a dict::

        {"tool_name": "...", "input": {...}, "call_id": "..."}

    Some older callers (and the SDK round-trip) use
    ``{"name": ..., "arguments": ..., "call_id"|"id": ...}``.
    Accept both shapes — fail silently if a record has neither.
    """
    raw = metadata.get("tool_calls")
    if raw is None and "tool_call" in metadata:
        raw = [metadata["tool_call"]]
    if not raw:
        return ()
    out: list[_ToolCallInternal] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        call_id = entry.get("call_id") or entry.get("id") or ""
        # Accept both legacy ("tool_name"/"input") and SDK ("name"/"arguments").
        name = entry.get("name") or entry.get("tool_name") or ""
        arguments = entry.get("arguments")
        if arguments is None:
            arguments = entry.get("input")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        if not name or not call_id:
            continue
        out.append(_ToolCallInternal(call_id=str(call_id), name=str(name), arguments=arguments))
    return tuple(out)


# ---------------------------------------------------------------------------
# SDK -> legacy
# ---------------------------------------------------------------------------


def sdk_to_legacy_item(m: Message) -> MemoryMessage | Placeholder | None:
    """Convert an SDK ``Message`` back to the legacy mixed-snapshot form.

    Returns ``None`` for messages that have no legacy representation
    (e.g. unrenderable types in the future). Callers should drop them.
    """
    if isinstance(m, UserMessage):
        return MemoryMessage(role="user", content=m.text)
    if isinstance(m, ContextNote):
        return note_to_memory_message(m)
    if isinstance(m, AssistantMessage):
        meta: dict = {}
        if m.tool_calls:
            # 与 harness 产出的 assistant metadata 同形(tool_name / input / call_id),
            # provider 只需认这一种;否则跨 run 回放时工具名与参数会丢。
            meta["tool_calls"] = [
                {"call_id": tc.call_id, "tool_name": tc.name, "input": tc.arguments}
                for tc in m.tool_calls
            ]
        if m.reasoning:
            meta["reasoning_content"] = m.reasoning
        return MemoryMessage(role="assistant", content=m.text, metadata=meta)
    if isinstance(m, ToolResultMessage):
        meta: dict = {"tool_use_id": m.tool_use_id}
        if m.tool_name:
            meta["tool_name"] = m.tool_name
        meta["ok"] = m.ok
        if m.revealed_tools:
            meta["revealed_tools"] = list(m.revealed_tools)
        return MemoryMessage(role="tool", content=m.content, metadata=meta)
    if isinstance(m, RegularPlaceholder):
        return Placeholder(run_id=m._internal.run_id, label=m.label)
    if isinstance(m, CompactPlaceholder):
        return Placeholder(
            run_id=m._internal.run_id,
            label="",
            metadata={"compact_placeholder": True},
        )
    if isinstance(m, CompactSummary):
        # Render the full POST_COMPACT_TEMPLATE so the harness emits the
        # same text the SDK would have sent to the model.
        try:
            content = m._render()
        except Exception:
            content = m.summary
        return MemoryMessage(
            role="compact",
            content=content,
            metadata={"compact_summary": True},
        )
    return None


def sdk_buffer_to_legacy_mixed(
    buf: Iterable[Message],
) -> tuple[MemoryMessage | Placeholder, ...]:
    out: list[MemoryMessage | Placeholder] = []
    for m in buf:
        item = sdk_to_legacy_item(m)
        if item is not None:
            out.append(item)
    return tuple(out)


def sdk_buffer_to_legacy_pure(buf: Iterable[Message]) -> tuple[MemoryMessage, ...]:
    """Returns the MemoryMessage-only subset (drops placeholders), in order."""
    return tuple(
        x for x in sdk_buffer_to_legacy_mixed(buf) if isinstance(x, MemoryMessage)
    )
