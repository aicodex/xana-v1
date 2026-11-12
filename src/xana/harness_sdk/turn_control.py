"""One service-owned execution per session; ordered, durable steering inputs.

Stopping an observer never stops its worker. Steering wakes observation tools;
only AbortSignal requests cancellation of owned execution.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Literal
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace

from uuid import uuid4
from xana.harness_sdk.contracts import AbortSignal, HarnessContext, HarnessEvent, HarnessRequest
from xana.multimodal.contracts import MediaInput
from xana.harness_sdk.events import RunEvents

Operation = Callable[[AbortSignal], Awaitable[object]]

@dataclass(frozen=True)
class AcceptedInput:
    id: str
    request: HarnessRequest
    delivery: Literal["queue", "steer"] = "queue"

@dataclass
class RunControl:
    session_id: str
    run_id: str
    agent_name: str
    abort: AbortSignal
    context: HarnessContext | None
    operations: list[tuple[Operation, asyncio.Future[object]]] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    finishing: bool = False
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.time)
    input_id: str = ""

Driver = Callable[[HarnessRequest, HarnessContext], AsyncGenerator[HarnessEvent, None]]
_END = object()

@dataclass
class EventFeed:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    observing: bool = True

    def put(self, event: object) -> None:
        if self.observing:
            self.queue.put_nowait(event)


@dataclass
class PreparedInput:
    context: HarnessContext
    driver: Driver
    events: EventFeed


class TurnCoordinator:
    def __init__(self, state_path: Path | None = None, *, events: RunEvents | None = None) -> None:
        self._path = state_path
        self._events = events
        self._lock = asyncio.Lock()
        self._active: dict[str, RunControl] = {}
        self._pending: dict[str, list[AcceptedInput]] = {}
        self._receipts: dict[str, set[str]] = {}
        self._closing = False
        self._prepared: dict[tuple[str, str], PreparedInput] = {}
        if state_path is not None and state_path.exists():
            data = json.loads(state_path.read_text())
            if data.get('version') != 1:
                raise ValueError('unsupported input queue format')
            self._receipts = {sid: set(ids) for sid, ids in data.get('receipts', {}).items()}
            for sid, rows in data['pending'].items():
                self._pending[sid] = [AcceptedInput(r['id'], HarnessRequest(sid, r['agent_name'], r['text'],
                    media=tuple(MediaInput(**m) for m in r['media']), metadata=r.get('metadata', {})), r.get("delivery", "queue")) for r in rows]

    def _save(self) -> None:
        if self._path is None:
            return
        data = {'version': 1, 'receipts': {sid: sorted(ids) for sid, ids in self._receipts.items()}, 'pending': {sid: [{'id': item.id, 'agent_name': item.request.agent_name,
            'text': item.request.user_input, 'delivery': item.delivery, 'media': [asdict(m) for m in item.request.media],
            'metadata': {k: v for k, v in item.request.metadata.items() if k in {'source', 'origin', 'parent_run_id'}}}
            for item in rows] for sid, rows in self._pending.items() if rows}}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._path)
        descriptor = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def active(self, session_id: str | None = None) -> tuple[RunControl, ...]:
        return tuple(c for sid, c in self._active.items() if session_id is None or sid == session_id)

    def pending_requests(self) -> tuple[HarnessRequest, ...]:
        return tuple(replace(item.request, metadata={**item.request.metadata, "input_id": item.id})
            for items in self._pending.values() for item in items)

    def pending_inputs(self, session_id: str) -> tuple[AcceptedInput, ...]:
        return tuple(deepcopy(item) for item in self._pending.get(session_id, ()))

    async def close_admission(self) -> None:
        async with self._lock:
            self._closing = True

    async def cancel(self, session_id: str, run_id: str | None = None, reason: str = 'user_cancel',
                     *, wait: bool = False) -> bool:
        async with self._lock:
            current = self._active.get(session_id)
            if current is None or (run_id is not None and current.run_id != run_id):
                return False
            accepted = not current.finishing
            if accepted:
                current.abort.trigger(reason)
            task = current.task
        if wait and task is not None:
            await asyncio.shield(task)
        return accepted

    async def claim_finish(self, control: RunControl) -> bool:
        async with self._lock:
            if (self._ready_inputs(control) or control.operations) and not control.abort.aborted:
                return False
            control.finishing = True
            return True

    async def seal(self, control: RunControl) -> tuple[AcceptedInput, ...]:
        async with self._lock:
            control.finishing = True
            return self._ready_inputs(control)

    async def inputs(self, control: RunControl) -> tuple[AcceptedInput, ...]:
        async with self._lock:
            control.wakeup.clear()
            return self._ready_inputs(control)

    def has_receipt(self, session_id: str, input_id: str) -> bool:
        return input_id in self._receipts.get(session_id, ())

    async def acknowledge(self, session_id: str, input_id: str) -> None:
        async with self._lock:
            sid = session_id
            previous = self._pending.get(sid, [])
            self._pending[sid] = [p for p in previous if p.id != input_id]
            receipts = self._receipts.setdefault(sid, set())
            existed = input_id in receipts
            receipts.add(input_id)
            try:
                self._save()
            except OSError:
                self._pending[sid] = previous
                if not existed:
                    receipts.discard(input_id)
                raise

    def _ready_inputs(self, control: RunControl) -> tuple[AcceptedInput, ...]:
        return tuple(sorted((item for item in self._pending.get(control.session_id, ())
            if item.id == control.input_id or item.delivery == "steer"),
            key=lambda item: item.request.metadata.get("source") == "background"))

    def _start_next(self, session_id: str) -> None:
        if self._closing or session_id in self._active:
            return
        items = self._pending.get(session_id, ())
        if not items:
            return
        item = items[0]
        prepared = self._prepared.pop((session_id, item.id), None)
        if prepared is None:
            return  # Recovery reconstructs dependencies through the normal request mapper.
        request, context = item.request, prepared.context
        if request.metadata.get("resume_run_id"):
            self._pending[session_id].pop(0)
        run_id = context.run_id or f"run-{uuid4().hex}"
        control = RunControl(session_id, run_id, request.agent_name, context.abort, context, input_id=item.id)
        context = replace(context, run_id=run_id, control=control)
        control.context = context
        self._active[session_id] = control
        control.task = asyncio.create_task(self._drive(request, context, prepared.driver, prepared.events),
                                           name=f"xana-turn-{run_id}")

    async def promote(self, session_id: str, input_id: str) -> None:
        async with self._lock:
            current = self._active.get(session_id)
            if current is None or current.finishing or current.abort.aborted or current.context is None:
                raise ValueError("没有可接收插话的活动回答")
            rows = self._pending.get(session_id, [])
            index = next((index for index, item in enumerate(rows) if item.id == input_id), None)
            if index is None or input_id == current.input_id:
                raise LookupError("消息已处理或不在队列中")
            item = rows[index]
            if item.delivery == "steer":
                return
            if item.request.agent_name != current.agent_name:
                raise ValueError("不能向另一 agent 的活动回答插话")
            await self._validate_media(item.request, current.context)
            rows[index] = replace(item, delivery="steer")
            try:
                self._save()
            except OSError:
                rows[index] = item
                raise
            prepared = self._prepared.pop((session_id, input_id), None)
            if prepared is not None:
                prepared.events.put(HarnessEvent("input_promoted", session_id, prepared.context.run_id or "",
                    {"input_id": input_id, "delivery": "steer", "text": "已插话，将在当前步骤完成后处理。"}))
                prepared.events.put(_END)
            current.wakeup.set()
            if self._events is not None:
                self._events.publish(HarnessEvent("input_promoted", session_id, current.run_id,
                                                  {"input_id": input_id, "delivery": "steer"}))

    async def invoke(self, request: HarnessRequest, context: HarnessContext, driver: Driver) -> AsyncGenerator[HarnessEvent, None]:
        events = None
        accepted = None
        while True:
            wait_for = None
            async with self._lock:
                if self._closing:
                    raise RuntimeError("服务正在停止，消息尚未接收，请稍后重试")
                current = self._active.get(request.session_id)
                if current is not None and (current.finishing or current.abort.aborted or current.context is None):
                    wait_for = current.task
                else:
                    automatic = request.metadata.get("source") in {"background", "goal"}
                    selected = current.context if automatic and current is not None else context
                    if current is not None and current.agent_name != request.agent_name:
                        raise ValueError("cannot change the active agent within a session")
                    await self._validate_media(request, selected)
                    ident = str(request.metadata.get("input_id") or uuid4().hex)
                    rows = self._pending.setdefault(request.session_id, [])
                    previous = next((item for item in rows if item.id == ident), None)
                    if request.metadata.get("resume_run_id") and (current is not None or rows):
                        raise ValueError("当前会话有活动执行或排队消息，请先完成它们再恢复")
                    duplicate = ident in self._receipts.get(request.session_id, ()) or (
                        previous is not None and ((request.session_id, ident) in self._prepared
                            or (current is not None and (ident == current.input_id or previous.delivery == "steer"))))
                    if duplicate:
                        accepted = HarnessEvent("input_accepted", request.session_id, current.run_id if current else context.run_id,
                            {"input_id": ident, "duplicate": True, "text": "这条消息已接收。"})
                        break
                    request = replace(request, metadata={**request.metadata, "input_id": ident})
                    delivery = previous.delivery if previous else ("steer" if automatic else "queue")
                    if previous is None and not request.metadata.get("resume_run_id"):
                        rows.append(AcceptedInput(ident, request, delivery))
                        try:
                            self._save()
                        except OSError:
                            rows.pop()
                            raise
                    elif previous is not None:
                        # Recovery restores the public request, including its durable input identity.
                        rows[rows.index(previous)] = replace(previous, request=request)
                    if current is not None and delivery == "steer":
                        current.wakeup.set()
                        accepted = HarnessEvent("input_accepted", request.session_id, current.run_id,
                            {"input_id": ident, "delivery": delivery, "text": "已收到补充，将在当前步骤完成后处理。"})
                    else:
                        events = EventFeed()
                        self._prepared[(request.session_id, ident)] = PreparedInput(context, driver, events)
                        if current is not None or (rows and rows[0].id != ident):
                            accepted = HarnessEvent("input_accepted", request.session_id, context.run_id or "",
                                {"input_id": ident, "delivery": delivery, "text": "已排队，当前回答结束后处理。"})
                        # A resumed run already owns its committed input and has no queued row.
                        if request.metadata.get("resume_run_id") and not rows:
                            rows.append(AcceptedInput(ident, request, "queue"))
                        self._start_next(request.session_id)
                    break
            if wait_for is not None:
                with suppress(Exception):
                    await asyncio.shield(wait_for)
        try:
            if accepted is not None:
                accepted = replace(accepted, payload={**accepted.payload, "user_input": request.user_input,
                                                      "source": request.metadata.get("source", "user")})
                if self._events is not None:
                    self._events.publish(accepted)
                yield accepted
            if events is not None:
                while True:
                    event = await events.queue.get()
                    if event is _END:
                        return
                    yield event
        finally:
            if events is not None:
                events.observing = False
                while not events.queue.empty():
                    events.queue.get_nowait()

    async def maintenance(self, session_id: str, run_id: str, operation: Operation) -> object:
        while True:
            async with self._lock:
                if self._closing:
                    raise RuntimeError("服务正在停止，压缩请求尚未接收")
                current = self._active.get(session_id)
                if current is not None and (current.finishing or current.abort.aborted or current.context is None):
                    task = current.task
                elif current is not None:
                    future = asyncio.get_running_loop().create_future()
                    current.operations.append((operation, future))
                    current.wakeup.set()
                    task = future
                    break
                else:
                    current = RunControl(session_id, run_id, "chat", AbortSignal(), None)
                    self._active[session_id] = current
                    async def perform(owner=current):
                        try:
                            return await operation(owner.abort)
                        finally:
                            async with self._lock:
                                self._active.pop(session_id, None)
                    current.task = asyncio.create_task(perform(), name=f"xana-maintenance-{run_id}")
                    task = current.task
                    break
            with suppress(Exception):
                await asyncio.shield(task)
        return await asyncio.shield(task)

    async def operations(self, control: RunControl) -> None:
        while control.operations:
            operation, future = control.operations.pop(0)
            try:
                if control.abort.aborted:
                    raise ValueError("对话已取消，未执行压缩请求")
                result = await operation(control.abort)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    async def _validate_media(self, request: HarnessRequest, context: HarnessContext) -> None:
        if not request.media:
            return
        if context.model_registry is None:
            raise ValueError('model registry required for media input')
        client = await context.model_registry.get(context.config_snapshot['provider'])
        if client is None:
            raise ValueError('configured model provider unavailable')
        caps = await client.capabilities(context.model)
        if any(m.kind not in caps.input_modalities for m in request.media):
            raise ValueError(f'模型 {context.model} 不支持此媒体输入')

    async def _drive(self, request: HarnessRequest, context: HarnessContext, driver: Driver, events: EventFeed) -> None:
        try:
            async for event in driver(request, context):
                if self._events is not None:
                    self._events.publish(event)
                events.put(event)
        except Exception as exc:
            failure = HarnessEvent('error', request.session_id, context.run_id or '', {'error': str(exc)})
            if self._events is not None:
                self._events.publish(failure)
            events.put(failure)
        finally:
            for _, future in context.control.operations:
                if not future.done():
                    future.set_exception(ValueError("对话已结束，未执行压缩请求"))
            async with self._lock:
                self._active.pop(request.session_id, None)
                if self._events is not None:
                    self._events.publish(HarnessEvent('run_closed', request.session_id, context.run_id or '', {}))
                events.put(_END)
                self._start_next(request.session_id)
