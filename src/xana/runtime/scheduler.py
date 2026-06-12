"""FileSchedulerService — extension_api.capabilities.SchedulerService 的文件持久化实现。

存储 ~/.xana/scheduled_tasks.json(boot 注入实际路径)。每条任务:
{id, cron, prompt, recurring, session_id, created_at, next_fire}
next_fire 用 croniter 由 cron 表达式算出(本地时区)。SchedulerRunner(serve 内
asyncio 任务)定期调 pop_due() 并把 prompt 经 gateway 注入对应 session。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from croniter import croniter

_logger = logging.getLogger(__name__)

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
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 半写损坏的任务文件不能杀死 runner/serve;丢弃并从空开始
            _logger.warning("scheduled_tasks 文件损坏,忽略并重建: %s", self._path)
            return []

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
                # 滚动副本入库:返回给调用方的 due 条目保留触发时刻的 next_fire
                rolled = dict(j)
                rolled["next_fire"] = croniter(j["cron"], now).get_next(float)
                keep.append(rolled)
        self._save(keep)
        return tuple(due)


class SchedulerRunner:
    """serve 内的轮询器:每 poll_interval_s 调一次 tick(),把到点任务的 prompt
    经 dispatch 回调(gateway 注入)送进任务记录的 session。dispatch 异常只记日志
    ——一个失败任务不能杀死 runner。"""

    def __init__(self, *, scheduler: FileSchedulerService,
                 dispatch: Callable[..., Awaitable[None]],
                 poll_interval_s: float = 30.0) -> None:
        self._scheduler = scheduler
        self._dispatch = dispatch
        self._interval = poll_interval_s
        self._task: asyncio.Task | None = None

    async def tick(self, *, now_epoch: float | None = None) -> None:
        for job in await self._scheduler.pop_due(now_epoch=now_epoch):
            try:
                await self._dispatch(session_id=job["session_id"], text=job["prompt"])
            except Exception:
                _logger.exception("scheduled job %s dispatch failed", job["id"])

    def start(self) -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self.tick()
                except Exception:
                    # tick 自身的意外异常(如 pop_due 失败)不能悄悄杀死轮询任务
                    _logger.exception("SchedulerRunner tick 意外失败")
        self._task = asyncio.create_task(_loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
