"""后台任务:超时转后台、长轮询等待、完成唤醒、硬上限(agent_run / eval_run 共用)。

一个任务 = 一个在后台运行的协程(子 agent 运行、一批评测)。工具先阻塞等待一段时间:
- 期间完成 → 直接把结果交给调用方(不再通知);
- 没完成 → 工具返回任务 id,任务继续运行;调用方可用 task_wait 长轮询,或去做别的事。
完成时的通知按父 run 的状态送达:
1. 父 run 正在 task_wait 这个任务 → 等待直接返回结果;
2. 父 run 仍在运行 → 排队,harness 在它下一次调用模型前作为注记插入(pop_finished_for_run);
3. 父 run 已结束 → 经注入的 deliver 往父会话发一条通知,触发父会话的 agent 接着处理。
硬上限:任务运行超过 hard_timeout_s 被取消,状态 timeout。
登记表落盘(state_path):serve 重启时仍在运行的任务标为 interrupted,并通知父会话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_logger = logging.getLogger(__name__)

TaskStatus = Literal["running", "completed", "failed", "cancelled", "timeout", "interrupted"]
DEFAULT_HARD_TIMEOUT_S = 3 * 3600.0
_PROGRESS_KEEP = 8

Deliver = Callable[[str, str], Awaitable[None]]          # (会话 id, 通知文本)
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
    delivered: bool = False        # 结果是否已交给父 run(等待返回 / 注记 / 会话通知)

    def summary(self) -> str:
        took = (self.finished or time.time()) - self.started
        return f"后台任务 {self.id}({self.kind}:{self.label})· 状态 {self.status} · 已运行 {took:.0f} 秒"

    def notification(self) -> str:
        return f"[后台任务结束] {self.summary()}\n{self.rendered or '(没有输出)'}"

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "kind", "label", "parent_session", "parent_run", "started",
                                               "status", "finished", "delivered")}


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

    # ---- 启动与运行 ----

    def start(self, *, kind: str, label: str, parent_session: str, parent_run: str,
              run: Callable[[Progress], Awaitable[TaskOutcome]], hard_timeout_s: float | None = None) -> TaskRecord:
        rec = TaskRecord(id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, label=label, parent_session=parent_session,
                         parent_run=parent_run, started=time.time())
        self._records[rec.id] = rec
        self._done[rec.id] = asyncio.Event()
        self._ended_runs.discard(parent_run)

        def progress(text: str) -> None:
            rec.progress = [*rec.progress[-(_PROGRESS_KEEP - 1):], text]

        task = asyncio.create_task(
            self._run(rec, run, progress, hard_timeout_s or self._hard), name=f"xana-bg-{rec.id}")
        task.add_done_callback(lambda t, rec=rec: self._cancelled_before_start(t, rec))
        self._tasks[rec.id] = task
        self._save()
        return rec

    def _cancelled_before_start(self, task: asyncio.Task, rec: TaskRecord) -> None:
        """还没开始执行就被取消(协程体没运行):补记状态,否则等待方永远等不到。"""
        if task.cancelled() and rec.status == "running":
            rec.status, rec.finished, rec.rendered = "cancelled", time.time(), "已停止(尚未开始)"
            self._done[rec.id].set()
            self._save()
            if rec.parent_run in self._ended_runs and not self._waiting.get(rec.id):
                asyncio.ensure_future(self._notify_session(rec))

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
        self._done[rec.id].set()
        self._save()
        await self._route(rec)

    async def _with_hard_timeout(self, coro: Awaitable[TaskOutcome], hard: float) -> TaskOutcome:
        """硬上限只计非冻结时间:额度冻结期间截止时间顺延。"""
        task = asyncio.ensure_future(coro)
        deadline = time.monotonic() + hard
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=min(30.0, max(0.0, deadline - time.monotonic())))
                if done:
                    return task.result()
                if self._freeze is not None and self._freeze.active:
                    deadline += 30.0
                elif time.monotonic() >= deadline:
                    task.cancel()
                    raise asyncio.TimeoutError
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _route(self, rec: TaskRecord) -> None:
        """完成后的送达:有人在等 → 由等待方取走;父 run 仍在运行 → 留给它的下一轮;父 run 已结束 → 通知父会话。"""
        if self._waiting.get(rec.id) or rec.parent_run not in self._ended_runs:
            return
        await self._notify_session(rec)

    async def _notify_session(self, rec: TaskRecord) -> None:
        if rec.delivered:
            return
        rec.delivered = True
        self._save()
        if self._deliver is None:
            return
        try:
            await self._deliver(rec.parent_session, rec.notification())
        except Exception:
            _logger.exception("后台任务 %s 的结果送达会话 %s 失败", rec.id, rec.parent_session)

    # ---- 查询、等待、停止 ----

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    async def wait(self, task_id: str, timeout_s: float) -> TaskRecord:
        """等到任务结束或超时;结束则视为已送达(调用方拿到了结果)。不存在抛 KeyError。"""
        rec = self._records[task_id]
        if rec.status == "running":
            self._waiting[task_id] = self._waiting.get(task_id, 0) + 1
            try:
                await asyncio.wait_for(asyncio.shield(self._done[task_id].wait()), timeout=max(0.0, timeout_s))
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiting[task_id] -= 1
        if rec.status != "running" and not rec.delivered:
            rec.delivered = True
            self._save()
        return rec

    def stop(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def running_for_run(self, run_id: str) -> list[TaskRecord]:
        return [r for r in self._records.values() if r.parent_run == run_id and r.status == "running"]

    # ---- harness 接入 ----

    def pop_finished_for_run(self, run_id: str) -> list[TaskRecord]:
        """父 run 运行中:取走它启动、已结束但还没送达的任务(harness 作为注记插入)。"""
        out = [r for r in self._records.values()
               if r.parent_run == run_id and r.status != "running" and not r.delivered and not self._waiting.get(r.id)]
        for r in out:
            r.delivered = True
        if out:
            self._save()
        return out

    async def run_ended(self, run_id: str) -> None:
        """父 run 结束:之后结束的任务改为通知父会话;已结束未送达的现在就送。"""
        self._ended_runs.add(run_id)
        for r in [r for r in self._records.values() if r.parent_run == run_id and r.status != "running"]:
            await self._notify_session(r)

    # ---- 持久化与重启 ----

    def _save(self) -> None:
        if self._state is None:
            return
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state.with_suffix(".tmp")
            tmp.write_text(json.dumps([r.to_json() for r in self._records.values()], ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._state)
        except OSError:
            _logger.warning("后台任务登记表写入失败", exc_info=True)

    async def recover(self) -> list[TaskRecord]:
        """启动时调用:上次进程里仍在运行的任务标为 interrupted 并通知父会话。"""
        if self._state is None or not self._state.exists():
            return []
        try:
            rows = json.loads(self._state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        lost = []
        for row in rows:
            if row.get("status") != "running":
                continue
            rec = TaskRecord(**{**row, "status": "interrupted", "finished": time.time(), "delivered": False})
            rec.rendered = ("serve 重启,这个后台任务被中断了。子 agent 任务可以用 agent_run 的 resume 续跑;"
                            "评测批次需要重新运行。")
            self._records[rec.id] = rec
            self._done[rec.id] = asyncio.Event()
            self._done[rec.id].set()
            lost.append(rec)
        self._save()
        for rec in lost:
            await self._notify_session(rec)
        return lost
