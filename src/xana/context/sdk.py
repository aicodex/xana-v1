"""Conversation lifecycle: isolated reads, ordered writes, replay and compaction."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from collections import defaultdict
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import time

from xana.context.message import CompactSummary, Message
from xana.memory._message_bridge import memory_message_to_sdk, messages_to_memory, sdk_to_memory
from xana.memory.contracts import HistoryRow, MemoryMessage, ReplayResult
from xana.memory.interfaces import HistoryMirror

SummaryGenerator = Callable[[tuple[MemoryMessage, ...], int], Awaitable[str | None]]


def estimate_tokens(messages: Sequence[MemoryMessage]) -> int:
    """Heuristic context size; provider usage remains authoritative for triggering."""
    size = sum(len(m.content or "") + (len(json.dumps(m.metadata["tool_calls"], ensure_ascii=False))
               if m.metadata.get("tool_calls") else 0) for m in messages)
    # Media estimates are not model capacity or compaction-reserve formulas.
    reserve = sum(4096 if p.kind == "image" else 16384 for m in messages for p in m.media)
    return max(1, size // 4 + reserve)


def revealed_tools_from_messages(messages: Sequence[MemoryMessage]) -> set[str]:
    return {str(name) for message in messages for key in ("revealed_tools", "pre_compact_revealed_tools")
            for name in (message.metadata.get(key) or ())}


class ContextSDK:
    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], list[Message]] = defaultdict(list)
        self._writes: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._compactions: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def summary(run_id: str, text: str, transcript: str = "", revealed: tuple[str, ...] = ()) -> CompactSummary:
        return CompactSummary(text, run_id, transcript, revealed)

    async def snapshot(self, sid: str, agent: str) -> tuple[Message, ...]:
        async with self._writes[sid]:
            return deepcopy(tuple(self._buffers[sid, agent]))

    async def replace(self, sid: str, agent: str, messages: Sequence[Message]) -> None:
        async with self._writes[sid]:
            self._buffers[sid, agent] = deepcopy(list(messages))

    async def append(self, sid: str, agent: str, message: Message) -> None:
        async with self._writes[sid]:
            self._buffers[sid, agent].append(deepcopy(message))

    async def append_history(self, mirror: HistoryMirror, row: HistoryRow) -> None:
        """Durable row and runtime view advance together, including duplicate recovery."""
        captured = deepcopy(row)
        async def publish() -> None:
            async with self._writes[captured.session_id]:
                try:
                    created = await mirror.append(captured)
                except Exception:
                    rows = await mirror.read_rows(captured.session_id)
                    if any(r.row_id == captured.row_id for r in rows):
                        self._replace_rows(rows, captured.session_id, captured.agent_name)
                    raise
                if created:
                    message = memory_message_to_sdk(captured.payload)
                    if message is not None:
                        self._buffers[captured.session_id, captured.agent_name].append(deepcopy(message))
                else:
                    rows = await mirror.read_rows(captured.session_id)
                    self._replace_rows(rows, captured.session_id, captured.agent_name)
        await self._finish_commit(publish())

    def _replace_rows(self, rows: Sequence[HistoryRow], sid: str, agent: str) -> None:
        self._buffers[sid, agent] = [deepcopy(message) for row in rows
            if row.session_id == sid and row.agent_name == agent and row.kind != "system"
            if (message := memory_message_to_sdk(row.payload)) is not None]

    async def restore(self, rows: Sequence[HistoryRow]) -> ReplayResult:
        keys = {(r.session_id, r.agent_name) for r in rows}
        for sid, agent in keys:
            async with self._writes[sid]:
                self._replace_rows(rows, sid, agent)
        return ReplayResult(loaded_sessions=len(keys), loaded_rows=len(rows))

    @staticmethod
    async def _finish_commit(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @asynccontextmanager
    async def maintenance(self, sid: str) -> AsyncIterator[None]:
        async with self._compactions[sid]:
            yield

    async def compact(self, mirror: HistoryMirror, *, session_id: str, agent_name: str,
                      compact_id: str, sessions_root: Path,
                      summarize: SummaryGenerator) -> CompactSummary | None:
        """Only this owner freezes, archives, validates and replaces a prefix.

        The summarizer receives an isolated value, not the live message objects.
        Concurrent appends are retained. Replacement/clear invalidates the commit.
        """
        async with self.maintenance(session_id):
            prefix = await self.snapshot(session_id, agent_name)
            messages = messages_to_memory(prefix)
            if not messages or (len(messages) == 1 and messages[0].role == "compact"):
                return None
            row_ids = await mirror.compaction_prefix(session_id, agent_name, messages)
            path = sessions_root / session_id / f"compact-{int(time.time() * 1_000_000)}.jsonl"
            overhead = estimate_tokens((MemoryMessage("compact", CompactSummary.render("", str(path))),))
            limit = estimate_tokens(messages) - overhead
            if limit <= 0:
                return None
            text = await summarize(deepcopy(messages), limit)
            if text is None:
                return None
            if not text.strip() or estimate_tokens((MemoryMessage("compact", text),)) >= limit:
                raise ValueError("summary does not reduce the active context")
            revealed = tuple(sorted(revealed_tools_from_messages(messages)))
            summary = self.summary(compact_id, text, str(path), revealed)
            async def publish() -> None:
                async with self._writes[session_id]:
                    current = self._buffers[session_id, agent_name]
                    if tuple(current[:len(prefix)]) != prefix:
                        raise ValueError("conversation prefix changed before compaction commit")
                    self._archive(path, messages)
                    try:
                        await mirror.compact(row_ids, HistoryRow(f"{compact_id}:compact", session_id,
                            agent_name, "compact", sdk_to_memory(summary), time.time(),
                            metadata={"covered_row_ids": list(row_ids)}))
                    except Exception:
                        rows = await mirror.read_rows(session_id)
                        if any(r.row_id == f"{compact_id}:compact" for r in rows):
                            self._replace_rows(rows, session_id, agent_name)
                        raise
                    current[:len(prefix)] = [summary]
            await self._finish_commit(publish())
            return summary

    @staticmethod
    def _archive(path: Path, messages: tuple[MemoryMessage, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            for message in messages:
                stream.write(json.dumps({"role": message.role, "content": message.content,
                    "metadata": dict(message.metadata),
                    **({"media": [asdict(p) for p in message.media]} if message.media else {})},
                    ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for directory in (path.parent, path.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
