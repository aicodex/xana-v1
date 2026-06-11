"""Per-session todo storage. JSON file at ~/.xana/sessions/{sid}.todos.json.

Mirrors cc's TodoWrite semantics: a single overwritable list per session.
TodoItem fields are content (imperative), active_form (present continuous),
and status (pending / in_progress / completed). Status semantics match cc.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass(frozen=True)
class TodoItem:
    content: str
    active_form: str
    status: TodoStatus


class TodoStore:
    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self.root / f"{session_id}.todos.json"

    def load(self, session_id: str) -> tuple[TodoItem, ...]:
        path = self._path(session_id)
        if not path.exists():
            return ()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return tuple(TodoItem(**item) for item in raw)

    def save(self, session_id: str, items: tuple[TodoItem, ...]) -> None:
        path = self._path(session_id)
        tmp = path.parent / (path.name + ".tmp")
        payload = [dataclasses.asdict(i) for i in items]
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
