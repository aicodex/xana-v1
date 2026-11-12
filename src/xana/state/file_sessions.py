"""Durable project session identity and parentage, independent of model context."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from xana.state.contracts import SessionRecord
from xana.state.interfaces import SessionStore


class FileSessionStore(SessionStore):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
            raise ValueError("invalid session ID")
        return self._root / f"{session_id}.json"

    async def get(self, session_id: str) -> SessionRecord | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        for field in ("cwd", "project_root"):
            if data.get(field) is not None:
                data[field] = Path(data[field])
        data["active_skills"] = tuple(data.get("active_skills", ()))
        return SessionRecord(**data)

    async def upsert(self, record: SessionRecord) -> None:
        path = self._path(record.session_id)
        data = asdict(record)
        for field in ("cwd", "project_root"):
            if data[field] is not None:
                data[field] = str(data[field])
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def patch(self, session_id: str, fields: dict) -> SessionRecord:
        record = await self.get(session_id)
        if record is None:
            raise KeyError(session_id)
        updated = replace(record, **fields)
        await self.upsert(updated)
        return updated

    async def list(self) -> tuple[SessionRecord, ...]:
        records = [await self.get(path.stem) for path in sorted(self._root.glob("*.json"))]
        return tuple(record for record in records if record is not None)

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)
