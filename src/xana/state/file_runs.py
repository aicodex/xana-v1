"""文件落盘的 RunStore：{root}/{run_id}.json + per-run JSONL writer (Task 4)。

new_run_id() 生成 8 字符 hex（与 scheduler 风格一致）；冲突由 FileRunStore.create
抛 FileExistsError 暴露，不内部重试。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, replace
from collections.abc import Callable
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
        parent_run_id=d.get("parent_run_id"),
        label=d.get("label") or "",
        budget=d.get("budget") or {},
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

    async def list_all(self, *, status: str | None = None) -> tuple[RunRecord, ...]:
        """返回全部记录,status 非 None 时按 status 过滤(§4.4 任务面板 +
        shutdown 遗留扫描;§2.2 已批准上 RunStore ABC)。"""
        recs = await self._iter()
        if status is not None:
            return tuple(r for r in recs if r.status == status)
        return tuple(recs)


RunTerminalStatus = Literal["completed", "cancelled", "failed"]


def prompt_path(runs_dir: Path, sha: str) -> Path:
    return Path(runs_dir) / "prompts" / f"{sha}.md"


def save_prompt(runs_dir: Path, text: str) -> str:
    """system prompt 按内容哈希存一份(runs/prompts/<sha>.md),返回 sha;trace 行只记 sha。"""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    path = prompt_path(runs_dir, sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, text)
    return sha


class RunJsonlWriter:
    """每个 run 写一个 {sessions_root}/runs/{run_id}.jsonl；finalize 写终止 marker。"""

    def __init__(self, sessions_root: Path, *, on_append: Callable[[str, HistoryRow], None] | None = None) -> None:
        self.sessions_root = Path(sessions_root)
        self._dir = self.sessions_root / "runs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._on_append = on_append
        self._row_ids: dict[str, set[str]] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise ValueError("invalid run ID")
        return self._dir / f"{run_id}.jsonl"

    async def append(self, run_id: str, row: HistoryRow, *, notify: bool = True) -> None:
        path = self._path(run_id)
        if run_id not in self._row_ids:
            transcript = self.read(run_id)
            self._row_ids[run_id] = {str(item["row_id"]) for item in transcript[0]} if transcript else set()
        if row.row_id in self._row_ids[run_id]:
            return
        try:
            self._write_line(path, _row_to_json(row))
        except BaseException:
            self._row_ids.pop(run_id, None)  # A failed fsync may follow a successful write.
            raise
        self._row_ids[run_id].add(row.row_id)
        if notify and self._on_append is not None:
            self._on_append(run_id, row)

    def read(self, run_id: str, *, errors: list[str] | None = None) -> tuple[list[dict], str | None] | None:
        self._path(run_id)  # Validate before the shared reader constructs a path.
        return read_run_transcript(self._dir, run_id, errors=errors)

    async def reconcile(self, rows: tuple[HistoryRow, ...]) -> None:
        """Repair main-history commits whose audit append was interrupted."""
        for row in rows:
            run_id = row.metadata.get("run_id")
            if isinstance(run_id, str) and run_id:
                await self.append(run_id, row, notify=False)
        self._row_ids.clear()

    @staticmethod
    def _write_line(path: Path, text: str) -> None:
        # Preserve a partial crash tail as its own invalid line, without swallowing the next row.
        with path.open("ab+") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write(text.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def finalize(self, run_id: str, status: RunTerminalStatus) -> None:
        marker = json.dumps(
            {"marker": "run_completed", "status": status, "ts": time.time()},
            ensure_ascii=False,
        )
        self._write_line(self._path(run_id), marker)
        self._row_ids.pop(run_id, None)


def read_run_transcript(runs_dir: Path, run_id: str, *, errors: list[str] | None = None) -> tuple[list[dict], str | None] | None:
    """读 runs/<run_id>.jsonl(续跑过的读 .aborted.jsonl)→ (行列表, 终止状态)。没有该 run 返回 None;损坏行跳过。"""
    runs_dir = Path(runs_dir)
    path = runs_dir / f"{run_id}.jsonl"
    if not path.exists():
        path = runs_dir / f"{run_id}.aborted.jsonl"
        if not path.exists():
            return None
    rows: list[dict] = []
    end_status: str | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            if errors is not None:
                errors.append(f"invalid transcript JSON at line {number}")
            continue
        if not isinstance(row, dict):
            if errors is not None:
                errors.append(f"invalid transcript record at line {number}")
        elif row.get("marker") == "run_completed":
            end_status = row.get("status")
        elif isinstance(row.get("payload"), dict) and isinstance(row.get("row_id"), str):
            rows.append(row)
        elif errors is not None:
            errors.append(f"unrecognized transcript record at line {number}")
    return rows, end_status
