"""文件落盘的 RunStore：{root}/{run_id}.json + per-run JSONL writer (Task 4)。

new_run_id() 生成 8 字符 hex（与 scheduler 风格一致）；冲突由 FileRunStore.create
抛 FileExistsError 暴露，不内部重试。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from xana.memory.contracts import HistoryRow
from xana.memory.file_history import _row_to_json
from xana.state.contracts import (
    EXCLUDED_EXIT_REASONS,
    RESUMABLE_EXIT_REASONS,
    Checkpoint,
    RunRecord,
    RunStatus,
    UsageSnapshot,
)
from xana.state.interfaces import RunStore


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _record_to_json(rec: RunRecord) -> str:
    d = asdict(rec)
    return json.dumps(d, ensure_ascii=False)


def _record_from_json(text: str) -> RunRecord:
    d = json.loads(text)
    cp = d.get("last_checkpoint")
    checkpoint = Checkpoint(**cp) if cp else None
    usage_d = d.get("usage") or {}
    usage = UsageSnapshot(**usage_d)
    return RunRecord(
        run_id=d["run_id"],
        session_id=d["session_id"],
        agent_name=d["agent_name"],
        status=d["status"],
        input_request=d.get("input_request") or {},
        last_checkpoint=checkpoint,
        usage=usage,
        pause_reason=d.get("pause_reason"),
        abort_reason=d.get("abort_reason"),
        error=d.get("error"),
        exit_reason=d.get("exit_reason"),
        metadata=d.get("metadata") or {},
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class FileRunStore(RunStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self._root / f"{run_id}.json"

    async def create(self, record: RunRecord) -> None:
        path = self._path(record.run_id)
        if path.exists():
            raise FileExistsError(f"RunRecord 已存在: {record.run_id}")
        _atomic_write(path, _record_to_json(record))

    async def get(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return _record_from_json(path.read_text(encoding="utf-8"))

    async def _read(self, run_id: str) -> RunRecord:
        rec = await self.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        return rec

    async def update_status(
        self, run_id: str, status: RunStatus, fields: dict | None = None
    ) -> RunRecord:
        rec = await self._read(run_id)
        patch: dict = {"status": status}
        if fields:
            patch.update(fields)
        updated = replace(rec, **patch)
        _atomic_write(self._path(run_id), _record_to_json(updated))
        return updated

    async def checkpoint(self, run_id: str, checkpoint: Checkpoint) -> None:
        rec = await self._read(run_id)
        updated = replace(rec, last_checkpoint=checkpoint)
        _atomic_write(self._path(run_id), _record_to_json(updated))

    async def update_usage(self, run_id: str, usage: UsageSnapshot) -> None:
        rec = await self._read(run_id)
        updated = replace(rec, usage=usage)
        _atomic_write(self._path(run_id), _record_to_json(updated))

    async def _iter(self) -> list[RunRecord]:
        out: list[RunRecord] = []
        for p in sorted(self._root.glob("*.json")):
            try:
                out.append(_record_from_json(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    async def list_unfinished(self) -> tuple[RunRecord, ...]:
        # P5 tightening: only records with a RESUMABLE exit_reason qualify.
        # status=="running" without exit_reason is an orphan from a crash —
        # startup_scan stamps shutdown_force on those before they show up here.
        out: list[RunRecord] = []
        for rec in await self._iter():
            if rec.exit_reason in EXCLUDED_EXIT_REASONS:
                continue
            if rec.exit_reason in RESUMABLE_EXIT_REASONS:
                out.append(rec)
        return tuple(out)

    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]:
        return tuple(r for r in await self._iter() if r.session_id == session_id)

    async def list_all(self) -> tuple[RunRecord, ...]:
        """Return every record. Not on the RunStore ABC — used by startup_scan
        to find orphan running/paused records that lack an exit_reason."""
        return tuple(await self._iter())


RunTerminalStatus = Literal["completed", "cancelled", "failed"]


class RunJsonlWriter:
    """每个 run 写一个 {sessions_root}/runs/{run_id}.jsonl；finalize 写终止 marker。"""

    def __init__(self, sessions_root: Path) -> None:
        self._dir = Path(sessions_root) / "runs"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.jsonl"

    async def append(self, run_id: str, row: HistoryRow) -> None:
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(_row_to_json(row) + "\n")

    def rename_to_aborted(self, run_id: str) -> None:
        """把 runs/{run_id}.jsonl 改名为 runs/{run_id}.aborted.jsonl(P5.3)。

        文件不存在时静默 noop —— resume 路径在已 aborted 的 run 上不应失败。
        """
        src = self._path(run_id)
        if not src.exists():
            return
        dst = self._dir / f"{run_id}.aborted.jsonl"
        src.rename(dst)

    async def finalize(self, run_id: str, status: RunTerminalStatus) -> None:
        marker = json.dumps(
            {"marker": "run_completed", "status": status, "ts": time.time()},
            ensure_ascii=False,
        )
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(marker + "\n")
