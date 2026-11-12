"""Append-only command output, paged by physical UTF-8 JSONL offsets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from collections.abc import Iterator
from typing import Literal

from xana.state.contracts import ProcessLogPage


class ProcessLogs:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, execution_id: str) -> Path:
        return self._root / (hashlib.sha256(execution_id.encode()).hexdigest() + ".jsonl")

    def start(self, execution_id: str, *, session_id: str, run_id: str, call_id: str, command: str) -> None:
        path = self._path(execution_id)
        with path.open("xb") as stream:
            stream.write((json.dumps({"type": "start", "execution_id": execution_id, "session_id": session_id,
                "run_id": run_id, "call_id": call_id, "command": command}, ensure_ascii=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def append(self, execution_id: str, stream: Literal["stdout", "stderr"], text: str) -> None:
        for index in range(0, len(text), 32_768):
            self._append(execution_id, {"type": "output", "stream": stream, "text": text[index:index + 32_768]})

    def finish(self, execution_id: str, exit_code: int | None) -> None:
        self._append(execution_id, {"type": "end", "exit_code": exit_code})

    def _append(self, execution_id: str, record: dict) -> None:
        with self._path(execution_id).open("ab") as stream:
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, execution_id: str, *, session_id: str, run_id: str,
             offset: int | None = None, before: int | None = None, page_bytes: int = 256 * 1024) -> ProcessLogPage:
        path = self._path(execution_id)
        if not path.exists():
            raise LookupError("process log not found")
        if offset is not None and before is not None:
            raise ValueError("choose offset or before")
        with path.open("rb") as stream:
            header = json.loads(stream.readline())
            if header.get("session_id") != session_id or header.get("run_id") != run_id:
                raise LookupError("process log not found")
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if offset is not None and not 0 <= offset <= size:
                raise ValueError("log offset out of range")
            if before is not None and not 0 <= before <= size:
                raise ValueError("log boundary out of range")
            if before:
                stream.seek(before - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("log boundary is not a record boundary")
            if offset is None:
                # A reader may start at the tail; skip a partial physical record at its boundary.
                start = max(0, (before if before is not None else size) - page_bytes)
                stream.seek(start)
                if start:
                    stream.readline()
                start = stream.tell()
            else:
                start = offset
                if start:
                    stream.seek(start - 1)
                    if stream.read(1) != b"\n":
                        raise ValueError("log offset is not a record boundary")
                stream.seek(start)
            records, issue = [], ""
            boundary = min(size, before) if before is not None else size
            while stream.tell() < boundary:
                position = stream.tell()
                raw = stream.readline()
                if not raw.endswith(b"\n"):
                    stream.seek(position)
                    issue = "incomplete log tail"
                    break
                record = json.loads(raw)
                if record.get("type") == "output":
                    records.append({"stream": record["stream"], "text": record["text"]})
                if stream.tell() - start >= page_bytes:
                    break
            cursor = stream.tell()
            stream.seek(max(0, size - 1024))
            tail = stream.read().splitlines()
            try:
                complete = bool(tail) and json.loads(tail[-1]).get("type") == "end"
            except (ValueError, AttributeError):
                complete = False
            return ProcessLogPage(tuple(records), start, cursor, cursor < boundary and not issue, complete, issue)

    def text(self, execution_id: str, *, session_id: str, run_id: str) -> Iterator[str]:
        """Stream complete plain output without loading the command log into memory."""
        with self._path(execution_id).open("r", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
            if header.get("session_id") != session_id or header.get("run_id") != run_id:
                raise LookupError("process log not found")
            for line in stream:
                if not line.endswith("\n"):
                    break
                record = json.loads(line)
                if record.get("type") == "output":
                    yield record["text"]
