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
    if role == "user":
        return UserMessage(text=mm.content)
    if role == "assistant":
        tool_calls = _parse_assistant_tool_calls(mm.metadata)
        return AssistantMessage(text=mm.content, tool_calls=tool_calls)
    if role == "tool":
        return ToolResultMessage(
            tool_use_id=str(mm.metadata.get("tool_use_id", "")),
            content=mm.content,
            ok=bool(mm.metadata.get("ok", True)),
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

    The legacy producer stores tool calls under one of two keys:
      - ``"tool_calls"``: tuple/list of dicts ``{call_id, name, arguments}``
      - ``"tool_call"`` (singular): a single dict in the same shape
    Anything that does not parse cleanly is silently dropped — the
    metadata is best-effort.
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
        name = entry.get("name") or ""
        arguments = entry.get("arguments")
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
    if isinstance(m, AssistantMessage):
        meta: dict = {}
        if m.tool_calls:
            meta["tool_calls"] = [
                {"call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ]
        return MemoryMessage(role="assistant", content=m.text, metadata=meta)
    if isinstance(m, ToolResultMessage):
        return MemoryMessage(
            role="tool",
            content=m.content,
            metadata={"tool_use_id": m.tool_use_id, "ok": m.ok},
        )
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
