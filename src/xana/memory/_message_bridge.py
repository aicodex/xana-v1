"""The current memory and ContextSDK message representations."""
from collections.abc import Iterable
from xana.context._internal import _ToolCallInternal
from xana.context.message import AssistantMessage, CompactSummary, ContextNote, Message, ToolResultMessage, UserMessage
from xana.memory.contracts import HistoryRow, MemoryMessage

def memory_message_to_sdk(message: MemoryMessage) -> Message | None:
    meta = message.metadata
    if message.role == "user":
        if meta.get("note_kind"):
            return ContextNote.from_metadata(meta)
        return UserMessage(message.content, message.media, str(meta.get("input_id", "")))
    if message.role == "assistant":
        calls = tuple(_ToolCallInternal(c["call_id"], c["tool_name"], c["input"]) for c in meta.get("tool_calls", ()))
        return AssistantMessage(message.content, calls, str(meta.get("reasoning_content", "")))
    if message.role == "tool":
        return ToolResultMessage(str(meta.get("tool_use_id", "")), message.content, bool(meta.get("ok", True)),
                                 str(meta.get("tool_name", "")), tuple(meta.get("revealed_tools", ())), message.media)
    if message.role == "compact":
        return CompactSummary(meta.get("summary", message.content), str(meta.get("run_id", "")),
                              str(meta.get("transcript_path", "")), tuple(meta.get("pre_compact_revealed_tools", ())))
    return None

def note_to_memory_message(note: ContextNote) -> MemoryMessage:
    return MemoryMessage("user", ContextNote.wrap(note.text), note.to_metadata())

def sdk_to_memory(message: Message) -> MemoryMessage:
    if isinstance(message, UserMessage):
        return MemoryMessage("user", message.text, {"input_id": message.input_id} if message.input_id else {}, message.media)
    if isinstance(message, ContextNote):
        return note_to_memory_message(message)
    if isinstance(message, AssistantMessage):
        meta = {}
        if message.tool_calls:
            meta["tool_calls"] = [{"call_id": c.call_id, "tool_name": c.name, "input": c.arguments} for c in message.tool_calls]
        if message.reasoning:
            meta["reasoning_content"] = message.reasoning
        return MemoryMessage("assistant", message.text, meta)
    if isinstance(message, ToolResultMessage):
        meta = {"tool_use_id": message.tool_use_id, "tool_name": message.tool_name, "ok": message.ok}
        if message.revealed_tools:
            meta["revealed_tools"] = list(message.revealed_tools)
        return MemoryMessage("tool", message.content, meta, message.media)
    if isinstance(message, CompactSummary):
        return MemoryMessage("compact", message._render(), {"compact_summary": True, "summary": message.summary,
            "transcript_path": message.transcript_path, "run_id": message.run_id,
            "pre_compact_revealed_tools": list(message.revealed_tools)})
    raise TypeError(f"unsupported context message: {type(message).__name__}")

def messages_to_memory(messages: Iterable[Message]) -> tuple[MemoryMessage, ...]:
    return tuple(sdk_to_memory(message) for message in messages)

def history_compaction_prefix(rows: Iterable[HistoryRow], session_id: str, agent_name: str,
                              expected: tuple[MemoryMessage, ...]) -> tuple[str, ...]:
    selected = [r for r in rows if (r.session_id, r.agent_name) == (session_id, agent_name)
                and r.kind != "system"][:len(expected)]
    normalized = tuple(sdk_to_memory(m) for r in selected if (m := memory_message_to_sdk(r.payload)) is not None)
    if normalized != expected:
        raise ValueError("history mirror does not match frozen compaction input")
    return tuple(r.row_id for r in selected)
