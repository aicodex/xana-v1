"""按会话持久追加消息，按捕获行 ID 提交前缀压缩，并从主日志恢复运行时视图。"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from dataclasses import replace, asdict
from xana.multimodal.contracts import MediaInput

from xana.memory.contracts import HistoryRow, MemoryMessage, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryReplayer

_logger = logging.getLogger(__name__)


def _row_to_json(row: HistoryRow) -> str:
    return json.dumps(
        {
            "row_id": row.row_id,
            "session_id": row.session_id,
            "agent_name": row.agent_name,
            "kind": row.kind,
            "ts": row.ts,
            "payload": {
                "role": row.payload.role,
                "content": row.payload.content,
                "metadata": row.payload.metadata,
                **({"media": [asdict(m) for m in row.payload.media]} if row.payload.media else {}),
            },
            "metadata": row.metadata,
        },
        ensure_ascii=False,
    )


def _row_from_json(line: str) -> HistoryRow:
    d = json.loads(line)
    p = d["payload"]
    return HistoryRow(
        row_id=d["row_id"],
        session_id=d["session_id"],
        agent_name=d["agent_name"],
        kind=d["kind"],
        payload=MemoryMessage(
            role=p["role"], content=p["content"], metadata=p.get("metadata") or {},
            media=tuple(MediaInput(**m) for m in p.get("media", ()))
        ),
        ts=d.get("ts", 0.0),
        metadata=d.get("metadata") or {},
    )


class FileHistoryMirror(HistoryMirror):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._row_ids: dict[str, set[str]] = {}

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    async def append(self, row: HistoryRow) -> bool:
        ids = self._row_ids.get(row.session_id)
        if ids is None:
            path = self._path(row.session_id)
            ids = set()
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        ids.add(json.loads(line)['row_id'])
                    except (ValueError, KeyError):
                        continue
            self._row_ids[row.session_id] = ids
        if row.row_id in ids:
            return False
        line = _row_to_json(row)
        with self._path(row.session_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        ids.add(row.row_id)
        return True

    async def read_rows(self, session_id: str | None = None) -> tuple[HistoryRow, ...]:
        rows: list[HistoryRow] = []
        paths = sorted(self._root.glob("*.jsonl")) if session_id is None else (self._path(session_id),)
        for path in paths:
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    _logger.warning("FileHistoryMirror: 跳过损坏行 %s", path.name)
                    continue
                if data.get("kind") not in {"user", "assistant", "tool", "system", "compact"}:
                    raise ValueError("obsolete history format; run the conversation migration before starting")
                try:
                    row = _row_from_json(raw)
                    rows.append(row)
                except Exception as exc:  # 损坏行容忍，不阻断恢复
                    _logger.warning(
                        "FileHistoryMirror: 跳过损坏行 %s: %s", path.name, exc
                    )
        return tuple(rows)

    async def replay(self) -> ReplayResult:
        rows = await self.read_rows()
        sessions = {r.session_id for r in rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(rows))




    async def compaction_prefix(self, session_id: str, agent_name: str,
                                expected: tuple[MemoryMessage, ...]) -> tuple[str, ...]:
        from xana.memory._message_bridge import history_compaction_prefix
        return history_compaction_prefix(await self.read_rows(session_id), session_id, agent_name, expected)

    async def compact(self, row_ids: tuple[str, ...], summary: HistoryRow) -> None:
        path = self._path(summary.session_id)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        selected = set(row_ids)
        valid_rows = []
        for line in lines:
            try:
                valid_rows.append(_row_from_json(line))
            except Exception:
                continue  # Preserve malformed source lines below.
        actual = tuple(r.row_id for r in valid_rows if r.agent_name == summary.agent_name and r.kind != "system")
        if summary.row_id in actual:
            return
        if actual[:len(row_ids)] != row_ids:
            raise ValueError("compaction prefix changed before commit")
        output: list[str] = []
        inserted = False
        for line in lines:
            try:
                row = _row_from_json(line)
            except Exception:
                output.append(line)
                continue
            if row.row_id == summary.row_id:
                return  # retry after durable commit
            matches = row.agent_name == summary.agent_name and row.row_id in selected
            if not inserted and (matches or (not selected and row.agent_name == summary.agent_name)):
                output.append(_row_to_json(summary))
                inserted = True
            if not matches:
                output.append(line)
        if not inserted:
            if selected:
                raise ValueError("compaction prefix no longer exists")
            output.append(_row_to_json(summary))
        self._write_rows(path, output)
        self._row_ids.setdefault(summary.session_id, set()).add(summary.row_id)

    @staticmethod
    def _write_rows(path: Path, lines: list[str]) -> None:
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)



class FileMemoryReplayer(MemoryReplayer):
    """读 mirror 全部行，按 (session_id, agent_name) 分组重建 ActiveContext。

    read_rows 是 HistoryMirror 正式契约；恢复不探测其他实现能力。
    """

    async def rebuild_active_context(
        self, mirror: HistoryMirror, store: ActiveContextStore
    ) -> ReplayResult:
        return await store.sdk().restore(await mirror.read_rows())
