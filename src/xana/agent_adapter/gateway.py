"""Channel translation around the single session execution service."""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import replace
from typing import AsyncIterator, Callable
from xana.agent_adapter.contracts import InboundMessage, RenderedMessage, SessionBinding, TaskPanelRow, CommandResult
from xana.agent_adapter.interfaces import Adapter, AgentGateway, AgentRequestRouter, CommandRouter
from xana.agents.contracts import AgentEvent
from xana.agents.interfaces import Agent, AgentRegistry
from xana.harness_sdk.turn_control import TurnCoordinator
from xana.state.interfaces import RunStore
from xana.state.file_runs import RunJsonlWriter

logger = logging.getLogger(__name__)

class DefaultAgentGateway(AgentGateway):
    def __init__(self, agents: dict[str, Agent], router: AgentRequestRouter,
                 agent_registry: AgentRegistry | None = None,
                 route_of: Callable[[str], tuple[str,str] | None] | None = None,
                 *, control: TurnCoordinator) -> None:
        self.agents, self.router = agents, router
        self.control = control
        self._agent_registry, self._route_of = agent_registry, route_of
        self.adapters: dict[str, Adapter] = {}
        self.command_router: CommandRouter | None = None
        self.run_store: RunStore | None = None
        self.run_jsonl_writer: RunJsonlWriter | None = None
        self._resume_inflight: set[str] = set()
        self._cmd_seq = 0
        self._deliveries: set[asyncio.Task[None]] = set()

    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None:
        self.adapters[adapter_id] = adapter

    async def register_command_router(self, router: CommandRouter) -> None:
        self.command_router = router

    def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        return self._submit(inbound)

    async def accept(self, inbound: InboundMessage) -> bool:
        receipt = asyncio.get_running_loop().create_future()

        async def render_until_end() -> None:
            try:
                async for rendered in self.submit(inbound):
                    if not receipt.done() and rendered.metadata.get("harness_kind") in {"turn_started", "input_accepted"}:
                        receipt.set_result(True)
            except Exception:
                logger.exception("automatic input delivery failed: session=%s", inbound.session_id)
            finally:
                if not receipt.done():
                    receipt.set_result(False)

        task = asyncio.create_task(render_until_end(), name=f"xana-delivery-{inbound.session_id}")
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)
        return await asyncio.shield(receipt)

    async def _submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        adapter = self.adapters.get(inbound.adapter_id)
        if adapter is None:
            yield RenderedMessage(inbound.adapter_id, inbound.session_id, f'unknown adapter {inbound.adapter_id}',
                                  metadata={'event_kind': 'error'})
            return
        if self.command_router is not None and inbound.text.strip().startswith('/'):
            self._cmd_seq += 1
            command_id = f'cmd-{inbound.session_id}-{self._cmd_seq}'
            inbound = replace(inbound, metadata={**inbound.metadata, 'command_run_id': command_id})
            if not inbound.text.strip().startswith(("/compact",)):
                adapter.note_progress({"kind": "command_progress", "session_id": inbound.session_id,
                    "run_id": command_id, "input_id": inbound.metadata.get("input_id"), "text": f"⏳ {inbound.text.strip().split()[0]}"})
            try:
                result = await self.command_router.route_command(inbound)
            except Exception as exc:
                logger.exception("command failed: %s", command_id)
                result = CommandResult(False, str(exc))
            if result.ok or not result.payload.get('pass_through'):
                if result.payload.get('silent'):
                    return
                event = AgentEvent('final' if result.ok else 'error', inbound.session_id,
                    payload={'text': result.message, 'run_id': command_id, 'input_id': inbound.metadata.get('input_id'), **result.payload})
                yield self._with_event_kind(await adapter.render(event), event)
                return
        request = await self.router.route(inbound)
        target = inbound.metadata.get('target_agent') or request.target_agent or ''
        if self._agent_registry is not None:
            await self._agent_registry.refresh()
        agent = self.agents.get(target)
        if agent is None:
            event = AgentEvent('error', inbound.session_id, payload={'text': f'agent {target!r} 不存在(可能已删除)，请用 /agent 选择。'})
            yield self._with_event_kind(await adapter.render(event), event)
            return
        try:
            # Drain through cleanup; the worker is service-owned, independently of this observer.
            async for event in agent.send(request):
                event = replace(event, payload={**event.payload, "input_id": inbound.metadata.get("input_id")})
                yield self._with_event_kind(await adapter.render(event), event)
        except Exception as exc:
            event = AgentEvent('error', inbound.session_id, payload={'text': str(exc), 'input_id': inbound.metadata.get('input_id')})
            yield self._with_event_kind(await adapter.render(event), event)

    async def list_inflight(self, session_id: str | None = None) -> tuple[TaskPanelRow, ...]:
        rows = {}
        if self.run_store is not None:
            records = await self.run_store.list_all() if session_id is None else await self.run_store.list_by_session(session_id)
            for rec in records:
                started = rec.metadata.get('started_at')
                end = rec.metadata.get('finished_at') or time.time()
                rows[rec.run_id] = TaskPanelRow(rec.run_id, rec.session_id, rec.label or 'run', rec.status,
                    max(0, end-started) if isinstance(started,(int,float)) else None, False)
        for current in self.control.active(session_id):
            rows[current.run_id] = TaskPanelRow(current.run_id, current.session_id, '当前对话', 'running',
                                               time.time()-current.started, True)
        return tuple(rows.values())

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        active = self.control.active(session_id)
        if await self.control.cancel(session_id, run_id):
            current = active[0]
            for adapter in self.adapters.values():
                adapter.note_progress({"kind": "run_stopping", "session_id": session_id, "run_id": current.run_id})

    async def resume(self, session_id: str, run_id: str) -> None:
        if run_id in self._resume_inflight:
            return
        if self.run_store is None:
            raise ValueError('run store unavailable')
        rec = await self.run_store.get(run_id)
        if rec is None or rec.session_id != session_id or rec.status not in {'paused','cancelled','failed'}:
            raise ValueError('只能恢复本会话中已停止的 run')
        route = self._route_of(session_id) if self._route_of else None
        adapter_id = route[0] if route else next(iter(self.adapters), '')
        if not adapter_id:
            raise ValueError('resume requires an available channel')
        self._resume_inflight.add(run_id)
        try:
            inbound = InboundMessage(adapter_id, session_id, rec.input_request.get('text',''),
                                     metadata={'resume_run_id': run_id})
            async for _ in self._submit(inbound):
                pass
        finally:
            self._resume_inflight.discard(run_id)

    async def recover_inputs(self) -> None:
        async def recover(request):
            if request.metadata.get("origin") == "agent_runner" or request.metadata.get("parent_run_id"):
                logger.info("内部执行的待处理输入保留，等待调用方显式恢复: session=%s", request.session_id)
                return
            route = self._route_of(request.session_id) if self._route_of else None
            adapter_id = route[0] if route else next(iter(self.adapters), "")
            if adapter_id not in self.adapters:
                logger.warning("已接收消息等待渠道恢复: session=%s", request.session_id)
                return
            if route:
                await self.adapters[adapter_id].bind_session(SessionBinding(adapter_id, route[1], request.session_id))
            inbound = InboundMessage(adapter_id, request.session_id, request.user_input,
                attachments=tuple({"file_path": m.path} for m in request.media),
                metadata={**request.metadata, "target_agent": request.agent_name})
            async for _ in self._submit(inbound):
                pass
        await asyncio.gather(*(recover(request) for request in self.control.pending_requests()))

    async def cancel_all(self) -> None:
        await self.control.close_admission()
        for current in self.control.active():
            await self.control.cancel(current.session_id, current.run_id, 'shutdown_graceful')

    def has_active_runs(self) -> bool:
        return bool(self.control.active() or self._deliveries)

    @staticmethod
    def _with_event_kind(rendered: RenderedMessage, event: AgentEvent) -> RenderedMessage:
        return replace(rendered, metadata={**rendered.metadata, 'event_kind': event.kind,
            'harness_kind': event.harness_event.kind if event.harness_event else None,
            'run_id': event.harness_event.run_id if event.harness_event else event.payload.get('run_id')})
