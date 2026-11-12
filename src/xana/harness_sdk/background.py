"""后台任务:超时转后台、长轮询等待、完成唤醒、硬上限(agent_run / eval_run 共用)。

一个任务 = 一个在后台运行的协程(子 agent 运行、一批评测)。工具先阻塞等待一段时间:
- 期间完成 → 直接把结果交给调用方(不再通知);
- 没完成 → 工具返回任务 id,任务继续运行;调用方可用 task_wait 长轮询,或去做别的事。
完成时的通知按父 run 的状态送达:
1. 父 run 正在 task_wait 这个任务 → 等待直接返回结果;
2. 父 run 仍在运行 → 排队,harness 在它下一次调用模型前作为注记插入;
3. 父 run 已结束 → 经注入的 deliver 往父会话发一条通知,触发父会话的 agent 接着处理。
硬上限:任务运行超过 hard_timeout_s 被取消,状态 timeout。
登记表落盘(state_path):serve 重启时仍在运行的任务标为 interrupted,并通知父会话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from xana.harness_sdk.contracts import AbortSignal, TaskNotification

_logger = logging.getLogger(__name__)

TaskStatus = Literal["running", "completed", "failed", "cancelled", "timeout", "interrupted"]
DEFAULT_HARD_TIMEOUT_S = 3 * 3600.0
_PROGRESS_KEEP = 8

Deliver = Callable[[TaskNotification], Awaitable[bool]]  # True = 持久队列已接收，非模型已处理
Progress = Callable[[str], None]


class QuotaFreeze:
    """模型额度用完时的全局冻结状态:harness 冻结非聊天 run 时设置,后台任务的硬上限计时在冻结期间暂停
    (否则冻结中的子 agent 会被当成跑不完而中止)。"""

    def __init__(self) -> None:
        self.until = 0.0

    def freeze(self, until: float) -> None:
        self.until = max(self.until, until)

    @property
    def active(self) -> bool:
        return time.time() < self.until


@dataclass(frozen=True)
class TaskOutcome:
    """任务协程的结果:ok、给模型看的文本、可选的结构化内容。"""
    ok: bool
    rendered: str
    content: dict = field(default_factory=dict)


@dataclass
class TaskRecord:
    id: str
    kind: str                      # agent / eval
    label: str
    parent_session: str
    parent_run: str
    started: float
    status: TaskStatus = "running"
    finished: float | None = None
    rendered: str = ""
    content: dict = field(default_factory=dict)
    progress: list[str] = field(default_factory=list)
    notify: bool = True           # 显式取消父回答后保留结果，等待用户下一轮读取
    delivered: bool = False        # 结果是否已交给父 run(等待返回 / 注记 / 会话通知)

    def summary(self) -> str:
        took = (self.finished or time.time()) - self.started
        return f"后台任务 {self.id}({self.kind}:{self.label})· 状态 {self.status} · 已运行 {took:.0f} 秒"

    def notification(self) -> TaskNotification:
        return TaskNotification(self.id, self.parent_session,
            f"[后台任务结束] {self.summary()}\n{self.rendered or '(没有输出)'}")

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "kind", "label", "parent_session", "parent_run", "started",
                                               "status", "finished", "delivered", "notify", "rendered", "content")}


class BackgroundTasks:
    def __init__(self, *, deliver: Deliver | None = None, state_path: Path | None = None,
                 hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S, freeze: QuotaFreeze | None = None) -> None:
        self._deliver = deliver
        self._freeze = freeze
        self._state = Path(state_path) if state_path else None
        self._hard = hard_timeout_s
        self._records: dict[str, TaskRecord] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._waiting: dict[str, int] = {}
        self._ended_runs: set[str] = set()
        self._notifying: set[str] = set()
        self._stopping: set[str] = set()
        self._interrupted = self._load()

    # ---- 启动与运行 ----

    def start(self, *, kind: str, label: str, parent_session: str, parent_run: str,
              run: Callable[[Progress], Awaitable[TaskOutcome]], hard_timeout_s: float | None = None,
              content: dict | None = None) -> TaskRecord:
        rec = TaskRecord(id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, label=label, parent_session=parent_session,
                         parent_run=parent_run, started=time.time(), content=dict(content or {}))
        self._records[rec.id] = rec
        self._done[rec.id] = asyncio.Event()
        self._ended_runs.discard(parent_run)

        def progress(text: str) -> None:
            rec.progress = [*rec.progress[-(_PROGRESS_KEEP - 1):], text]

        try:
            self._save()
        except OSError:
            self._records.pop(rec.id)
            self._done.pop(rec.id)
            raise
        task = asyncio.create_task(
            self._run(rec, run, progress, hard_timeout_s or self._hard), name=f"xana-bg-{rec.id}")
        task.add_done_callback(lambda t, rec=rec: self._cancelled_before_start(t, rec))
        self._tasks[rec.id] = task
        return rec

    def _cancelled_before_start(self, task: asyncio.Task, rec: TaskRecord) -> None:
        """还没开始执行就被取消(协程体没运行):补记状态,否则等待方永远等不到。"""
        self._stopping.discard(rec.id)
        if task.cancelled() and rec.status == "running":
            rec.status, rec.finished, rec.rendered = "cancelled", time.time(), "已停止(尚未开始)"
            self._done[rec.id].set()
            self._save()
            if rec.parent_run in self._ended_runs and not self._waiting.get(rec.id):
                asyncio.create_task(self._notify_session(rec))

    async def _run(self, rec: TaskRecord, run: Callable[[Progress], Awaitable[TaskOutcome]], progress: Progress,
                   hard: float) -> None:
        try:
            out = await self._with_hard_timeout(run(progress), hard)
            rec.status, rec.rendered, rec.content = ("completed" if out.ok else "failed"), out.rendered, out.content
        except asyncio.TimeoutError:
            rec.status = "timeout"
            rec.rendered = f"超过硬上限 {hard:.0f} 秒,已中止。最近进度:\n" + "\n".join(rec.progress)
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.rendered = "已停止。最近进度:\n" + "\n".join(rec.progress)
        except Exception as exc:          # 任务内部异常也要作为结果送达
            _logger.exception("后台任务 %s 失败", rec.id)
            rec.status, rec.rendered = "failed", f"内部错误:{type(exc).__name__}: {exc}"
        rec.finished = time.time()
        try:
            self._save()
        except OSError:
            _logger.exception("后台任务 %s 的终态尚未持久化，保留结果待重试", rec.id)
        finally:
            self._done[rec.id].set()
        await self._route(rec)

    async def _with_hard_timeout(self, coro: Awaitable[TaskOutcome], hard: float) -> TaskOutcome:
        """硬上限只计非冻结时间:额度冻结期间截止时间顺延。"""
        task = asyncio.ensure_future(coro)
        deadline = time.monotonic() + hard
        cancel_requested = False
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=min(30.0, max(0.0, deadline - time.monotonic())))
                if done:
                    return task.result()
                if self._freeze is not None and self._freeze.active:
                    deadline += 30.0
                elif time.monotonic() >= deadline:
                    cancel_requested = True
                    task.cancel()
                    await asyncio.shield(asyncio.gather(task, return_exceptions=True))
                    raise asyncio.TimeoutError
        except asyncio.CancelledError:
            if not cancel_requested:
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise

    async def _route(self, rec: TaskRecord) -> None:
        """完成后的送达:有人在等 → 由等待方取走;父 run 仍在运行 → 留给它的下一轮;父 run 已结束 → 通知父会话。"""
        if self._waiting.get(rec.id) or rec.parent_run not in self._ended_runs:
            return
        await self._notify_session(rec)

    async def _notify_session(self, rec: TaskRecord) -> None:
        if rec.delivered or not rec.notify or self._deliver is None or rec.id in self._notifying:
            return
        self._notifying.add(rec.id)
        try:
            self._save()
            accepted = await self._deliver(rec.notification())
            if accepted:
                self.acknowledge(rec.id)
        except Exception:
            _logger.exception("后台任务 %s 的结果送达会话 %s 失败", rec.id, rec.parent_session)
        finally:
            self._notifying.discard(rec.id)

    # ---- 查询、等待、停止 ----

    def get(self, task_id: str, *, session_id: str | None = None) -> TaskRecord | None:
        rec = self._records.get(task_id)
        return rec if rec is not None and (session_id is None or rec.parent_session == session_id) else None

    def list_for_session(self, session_id: str, *, include_finished: bool = False) -> tuple[TaskRecord, ...]:
        return tuple(sorted((r for r in self._records.values() if r.parent_session == session_id
            and (include_finished or r.status == "running" or not r.delivered)),
            key=lambda r: r.started, reverse=True))

    async def wait(self, task_id: str, timeout_s: float, *, wakeup: asyncio.Event | None = None) -> TaskRecord:
        """只观察结果；持久写入历史或交给持久输入队列后才 acknowledge。"""
        rec = self._records[task_id]
        if rec.status == "running":
            self._waiting[task_id] = self._waiting.get(task_id, 0) + 1
            try:
                watchers = [asyncio.create_task(self._done[task_id].wait())]
                if wakeup is not None:
                    watchers.append(asyncio.create_task(wakeup.wait()))
                try:
                    await asyncio.wait(watchers, timeout=max(0.0, timeout_s), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for watcher in watchers:
                        watcher.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)
            finally:
                self._waiting[task_id] -= 1
        return rec

    async def stop(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.done() or self._records[task_id].status != "running":
            return False
        if task_id not in self._stopping:
            self._stopping.add(task_id)
            task.cancel()
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        return True

    def running_for_run(self, run_id: str) -> list[TaskRecord]:
        return [r for r in self._records.values() if r.parent_run == run_id and r.status == "running"]

    async def wait_for_run(self, run_id: str, *, wakeup: asyncio.Event, abort: AbortSignal) -> None:
        """Keep an unattended caller alive without spending model calls on polling."""
        pending = self.running_for_run(run_id)
        if not pending:
            return
        watchers = [asyncio.create_task(self._done[rec.id].wait()) for rec in pending]
        watchers.extend((asyncio.create_task(wakeup.wait()), asyncio.create_task(abort.wait())))
        try:
            await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    # ---- harness 接入 ----

    def finished_for_run(self, run_id: str, session_id: str) -> list[TaskRecord]:
        return [r for r in self._records.values() if not r.delivered and r.status != "running" and r.id not in self._notifying
                and not self._waiting.get(r.id) and
                (r.parent_run == run_id or ((not r.notify or r.parent_run in self._ended_runs) and r.parent_session == session_id))]

    def acknowledge(self, task_id: str) -> None:
        rec = self._records[task_id]
        previous = rec.delivered
        rec.delivered = True
        try:
            self._save()
        except OSError:
            rec.delivered = previous
            raise

    async def run_ended(self, run_id: str, *, cancelled: bool = False) -> None:
        self._ended_runs.add(run_id)
        records = [r for r in self._records.values() if r.parent_run == run_id]
        if cancelled:
            for rec in records:
                rec.notify = False
            self._save()
        await asyncio.gather(*(self._notify_session(rec) for rec in records if rec.status != "running"))

    async def retry_notifications(self, *, interval_s: float = 5.0) -> None:
        """Service-owned retry loop; caller cancels it during shutdown."""
        await self.recover()
        while True:
            await asyncio.sleep(interval_s)
            await asyncio.gather(*(self._route(rec) for rec in tuple(self._records.values())
                if rec.status != "running" and not rec.delivered))

    # ---- 持久化与重启 ----

    def _save(self) -> None:
        if self._state is None:
            return
        self._state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump([r.to_json() for r in self._records.values()], f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._state)
        descriptor = os.open(self._state.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load(self) -> list[TaskRecord]:
        """Load before new work can save the registry; never overwrite unrecovered records."""
        if self._state is None or not self._state.exists():
            return []
        rows = json.loads(self._state.read_text(encoding="utf-8"))
        lost = []
        for row in rows:
            rec = TaskRecord(**row)
            if rec.status == "running":
                rec.status, rec.finished, rec.delivered = "interrupted", time.time(), False
                rec.rendered = ("serve 重启,这个后台任务被中断了。子 agent 任务可以用 agent_run 的 resume 续跑;"
                                "评测批次需要重新运行。")
                if rec.content.get("agent_id"):
                    rec.rendered += f'\nagent_id={rec.content["agent_id"]}'
                lost.append(rec)
            self._records[rec.id] = rec
            self._ended_runs.add(rec.parent_run)
            self._done[rec.id] = asyncio.Event()
            self._done[rec.id].set()
        self._save()
        return lost

    async def recover(self) -> list[TaskRecord]:
        """Notify only after channels are ready; interrupted work is never replayed."""
        await asyncio.gather(*(self._route(rec) for rec in tuple(self._records.values())
            if rec.status != "running"))
        return list(self._interrupted)
