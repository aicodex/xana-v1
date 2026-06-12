"""FileSchedulerService — extension_api.capabilities.SchedulerService 的文件持久化实现。

存储 ~/.xana/scheduled_tasks.json(boot 注入实际路径)。每条任务:
{id, cron, prompt, recurring, session_id, created_at, next_fire}
next_fire 用 croniter 由 cron 表达式算出(本地时区)。SchedulerRunner(serve 内
asyncio 任务)定期调 pop_due() 并把 prompt 经 gateway 注入对应 session。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from croniter import croniter

from xana.extension_api.capabilities import SchedulerService


class FileSchedulerService(SchedulerService):
    def __init__(self, *, path: Path, session_id: str) -> None:
        self._path = path
        # 公有可变属性:cron 工具在 execute 时按真实 ToolContext.session_id 覆写;
        # runner 串行消费任务记录里的 session_id,无并发风险。
        self.session_id = session_id

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, jobs: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")

    async def add(self, *, cron: str, prompt: str, recurring: bool = False) -> str:
        if not croniter.is_valid(cron):
            raise ValueError(f"非法 cron 表达式: {cron!r}")
        now = time.time()
        task_id = uuid.uuid4().hex[:8]
        jobs = self._load()
        jobs.append({
            "id": task_id, "cron": cron, "prompt": prompt, "recurring": recurring,
            "session_id": self.session_id, "created_at": now,
            "next_fire": croniter(cron, now).get_next(float),
        })
        self._save(jobs)
        return task_id

    async def remove(self, task_id: str) -> None:
        self._save([j for j in self._load() if j["id"] != task_id])

    async def list_all(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._load())

    async def pop_due(self, *, now_epoch: float | None = None) -> tuple[dict[str, Any], ...]:
        """到点任务:一次性的删除,循环的滚动 next_fire。SchedulerRunner 专用。"""
        now = now_epoch if now_epoch is not None else time.time()
        jobs = self._load()
        due = [j for j in jobs if j["next_fire"] <= now]
        keep = [j for j in jobs if j["next_fire"] > now]
        for j in due:
            if j["recurring"]:
                j["next_fire"] = croniter(j["cron"], now).get_next(float)
                keep.append(j)
        self._save(keep)
        return tuple(due)
