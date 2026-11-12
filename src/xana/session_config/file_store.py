"""FileSessionConfigStore: JSON-per-session under ~/.xana/sessions/.

Atomic write via temp file + rename. Rejects session_ids containing path
separators or `..` to prevent traversal — slash-command handlers feed
user-controlled strings in, so the guard is load-bearing.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from xana.session_config.contracts import SessionConfig
from xana.session_config.interfaces import SessionConfigStore


class FileSessionConfigStore(SessionConfigStore):
    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id.split("/"):
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self.root / f"{session_id}.json"

    async def get(self, session_id: str) -> SessionConfig:
        path = self._path(session_id)
        if not path.exists():
            return SessionConfig(session_id=session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["permission_grants"] = tuple(data.get("permission_grants", ()))
        data["session_id"] = session_id
        return SessionConfig(**data)

    async def save(self, config: SessionConfig) -> None:
        path = self._path(config.session_id)
        tmp = path.with_suffix(".json.tmp")
        payload = dataclasses.asdict(config)
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
