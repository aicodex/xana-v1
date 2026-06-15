"""BindingStore: sidecar JSON mapping external_thread_id → session_id.

Each channel adapter owns one. Stores are loaded eagerly on construction
and saved atomically on every put — channel processes are long-lived but
race-tolerant.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


class BindingStore:
    def __init__(self, *, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, external_thread_id: str) -> str | None:
        return self._data.get(external_thread_id)

    def put(self, external_thread_id: str, session_id: str) -> None:
        self._data[external_thread_id] = session_id
        tmp = self.path.parent / (self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def allocate(self, external_thread_id: str, *, prefix: str) -> str:
        existing = self.get(external_thread_id)
        if existing is not None:
            return existing
        sid = f"{prefix}-{secrets.token_hex(6)}"
        self.put(external_thread_id, sid)
        return sid

    def rotate(self, external_thread_id: str, *, prefix: str) -> str:
        """Overwrite mapping with a fresh session_id. Used by /clear to give the
        user a truly fresh session — next inbound from the same external thread
        will allocate to (i.e. find) the new sid, abandoning all prior history.
        """
        sid = f"{prefix}-{secrets.token_hex(6)}"
        self.put(external_thread_id, sid)
        return sid
