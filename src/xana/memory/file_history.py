"""会话历史的 JSONL 持久化 + 启动恢复。

FileHistoryMirror：每 session 一个 {root}/{session_id}.jsonl，每行一条
序列化的 HistoryRow，append-only，跨进程重启不丢。
FileMemoryReplayer：读回 JSONL，重建 ActiveContext（Task 2）。
needs_continue / replay_and_detect_interrupts：CC 风格中断检测（Task 3）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from xana.memory.contracts import ActiveContext, HistoryRow, MemoryMessage, ReplayResult
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
            role=p["role"], content=p["content"], metadata=p.get("metadata") or {}
        ),
        ts=d.get("ts", 0.0),
        metadata=d.get("metadata") or {},
    )


class FileHistoryMirror(HistoryMirror):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    async def append(self, row: HistoryRow) -> None:
        line = _row_to_json(row)
        with self._path(row.session_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def read_rows(self) -> tuple[HistoryRow, ...]:
        rows: list[HistoryRow] = []
        for path in sorted(self._root.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(_row_from_json(raw))
                except Exception as exc:  # 损坏行容忍，不阻断恢复
                    _logger.warning(
                        "FileHistoryMirror: 跳过损坏行 %s: %s", path.name, exc
                    )
        return tuple(rows)

    async def replay(self) -> ReplayResult:
        rows = await self.read_rows()
        sessions = {r.session_id for r in rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(rows))


class FileMemoryReplayer(MemoryReplayer):
    """读 mirror 全部行，按 (session_id, agent_name) 分组重建 ActiveContext。

    依赖 mirror 暴露 async read_rows()（FileHistoryMirror / InMemory 均可加）；
    没有该方法的 mirror 视为无可恢复内容。
    """

    async def rebuild_active_context(
        self, mirror: HistoryMirror, store: ActiveContextStore
    ) -> ReplayResult:
        read_rows = getattr(mirror, "read_rows", None)
        if read_rows is None:
            return ReplayResult(loaded_sessions=0, loaded_rows=0)
        rows = await read_rows()
        grouped: dict[tuple[str, str], list[MemoryMessage]] = {}
        for row in rows:
            grouped.setdefault((row.session_id, row.agent_name), []).append(row.payload)
        for (session_id, agent_name), msgs in grouped.items():
            await store.set(
                ActiveContext(
                    session_id=session_id,
                    agent_name=agent_name,
                    messages=tuple(msgs),
                    token_estimate=sum(len(m.content) for m in msgs),
                )
            )
        return ReplayResult(loaded_sessions=len(grouped), loaded_rows=len(rows))
