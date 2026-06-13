"""DefaultAgentGateway: wires Adapter <-> Agent."""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncGenerator, AsyncIterator

from xana.agent_adapter.contracts import (
    InboundMessage,
    InflightRun,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import (
    Adapter,
    AgentGateway,
    AgentRequestRouter,
    CommandRouter,
)
from xana.agents.contracts import AgentEvent
from xana.agents.interfaces import Agent


class DefaultAgentGateway(AgentGateway):
    def __init__(
        self,
        agents: dict[str, Agent],
        router: AgentRequestRouter,
    ) -> None:
        self.agents = agents
        self.router = router
        self.adapters: dict[str, Adapter] = {}
        self.command_router: CommandRouter | None = None
        self.bindings: dict[str, SessionBinding] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._cmd_seq = 0
        self._inflight: dict[str, list[InflightRun]] = {}  # session_id → 倒序(新在前)
        self._run_seq = 0

    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None:
        self.adapters[adapter_id] = adapter

    async def register_command_router(self, router: CommandRouter) -> None:
        self.command_router = router

    async def attach(self, binding: SessionBinding) -> None:
        self.bindings[binding.session_id] = binding

    def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        return self._submit(inbound)

    async def _submit(self, inbound: InboundMessage) -> AsyncGenerator[RenderedMessage, None]:
        adapter = self.adapters.get(inbound.adapter_id)
        if adapter is None:
            yield RenderedMessage(
                adapter_id=inbound.adapter_id, session_id=inbound.session_id,
                text=f"[gateway] unknown adapter={inbound.adapter_id!r}",
                metadata={"event_kind": "error"},
            )
            return

        # Slash-command short-circuit: ask the command_router first. If it intercepts
        # (ok=True or ok=False with pass_through=False), render the result and return.
        if self.command_router is not None:
            # 命令拦截前先给命令分配 run_id 并发 command_progress 预建卡片,
            # 再把 run_id 经 metadata 传给 router(compact 等内部进度复用同卡),
            # final/error 把 run_id 放进 payload,adapter 按 run_id finalize 同卡。
            is_command = inbound.text.strip().startswith("/")
            run_id = ""
            enriched = inbound
            # /compact_cancel 是隐藏命令,只置取消事件,不该自成一张卡 →
            # 跳过预建(预建发生在路由前,故按命令名提前 gate)。
            silent_cmd = inbound.text.strip().startswith("/compact_cancel")
            if is_command and not silent_cmd:
                self._cmd_seq += 1
                run_id = f"cmd-{inbound.session_id}-{self._cmd_seq}"
                self._notify_command_card(adapter, inbound, run_id)
                enriched = replace(
                    inbound, metadata={**inbound.metadata, "command_run_id": run_id}
                )
            cmd_result = await self.command_router.route_command(enriched)
            if cmd_result.payload.get("triggers_turn", False):
                # Turn-triggering command (e.g. /compact): falls through to the
                # regular agent loop so it gets inflight tracking + detach semantics.
                pass
            elif cmd_result.ok or not cmd_result.payload.get("pass_through", False):
                # silent 结果(/compact_cancel)不渲染卡片,直接结束。
                if cmd_result.payload.get("silent"):
                    return
                event = AgentEvent(
                    kind="final" if cmd_result.ok else "error",
                    session_id=inbound.session_id,
                    payload={"text": cmd_result.message, "run_id": run_id, **cmd_result.payload},
                )
                rendered = await adapter.render(event)
                yield self._attach_event_kind(rendered, event)
                return

        request = await self.router.route(inbound)
        target = request.target_agent or ""
        agent = self.agents.get(target)
        if agent is None:
            yield RenderedMessage(
                adapter_id=inbound.adapter_id, session_id=inbound.session_id,
                text=f"[gateway] unknown agent={target!r}",
                metadata={"event_kind": "error"},
            )
            return
        # 旧 run 转后台 —— 不 cancel,只标 detached,并通知 adapter 刷 tab。
        existing = self._inflight.get(inbound.session_id, [])
        for ir in existing:
            if not ir.detached:
                ir.detached = True
                self._notify_run_detached(adapter, inbound.session_id, ir)

        self._run_seq += 1
        new_run = InflightRun(
            session_id=inbound.session_id,
            run_id=f"run-{inbound.session_id}-{self._run_seq}",
            label=(inbound.text or "").strip()[:40] or "run",
        )
        self._inflight.setdefault(inbound.session_id, []).insert(0, new_run)
        try:
            async for event in agent.send(request):
                rendered = await adapter.render(event)
                yield self._attach_event_kind(rendered, event)
                if event.kind in ("final", "error", "cancelled"):
                    break
        finally:
            self._inflight[inbound.session_id] = [
                ir for ir in self._inflight.get(inbound.session_id, [])
                if ir.run_id != new_run.run_id
            ]
            note = getattr(adapter, "note_progress", None)
            if note is not None:
                note({
                    "kind": "ph_replaced",
                    "session_id": inbound.session_id,
                    "run_id": new_run.run_id,
                    "mode": "full",
                })
                if new_run.detached:
                    # 这个 run 在跑的过程中被新的 submit detach 掉了 → 完成时通知 adapter
                    note({
                        "kind": "run_completed_bg",
                        "session_id": inbound.session_id,
                        "run_id": new_run.run_id,
                    })

    async def start_compact(self, session_id: str, fork_point: int) -> None:
        """Trigger a compact run. Stub: emits start/finalize events without running compact.
        Real compact execution wired in Task 8."""
        adapter_id = next(iter(self.adapters), None)
        if adapter_id is None:
            return
        adapter = self.adapters[adapter_id]
        note = getattr(adapter, "note_progress", None)
        if note is None:
            return
        self._run_seq += 1
        rid = f"compact-{session_id}-{self._run_seq}"
        note({"kind": "compact_started", "session_id": session_id, "run_id": rid, "fork_point": fork_point})
        note({"kind": "compact_finalized", "session_id": session_id, "run_id": rid, "summary": ""})

    async def switch_foreground(self, session_id: str, target_run_id: str) -> None:
        runs = self._inflight.get(session_id, [])
        if not any(ir.run_id == target_run_id for ir in runs):
            return
        for ir in runs:
            ir.detached = (ir.run_id != target_run_id)
        for ad in self.adapters.values():
            note = getattr(ad, "note_progress", None)
            if note is None:
                continue
            note({
                "kind": "foreground_changed",
                "session_id": session_id,
                "run_id": target_run_id,
            })

    @staticmethod
    def _notify_run_detached(adapter, session_id: str, ir: "InflightRun") -> None:
        note = getattr(adapter, "note_progress", None)
        if note is None:
            return
        note({
            "kind": "run_detached",
            "session_id": session_id,
            "run_id": ir.run_id,
            "label": ir.label,
        })

    @staticmethod
    def _notify_command_card(adapter: Adapter, inbound: InboundMessage, run_id: str) -> None:
        """命令路由前预建一张卡:发 command_progress ProgressEvent。
        console 等无 note_progress 的 adapter 直接跳过(hasattr 守卫)。"""
        note = getattr(adapter, "note_progress", None)
        if note is None:
            return
        first = inbound.text.strip().split()[0] if inbound.text.strip() else ""
        note({
            "kind": "command_progress",
            "run_id": run_id,
            "session_id": inbound.session_id,
            "text": f"⏳ {first}",
        })

    def subscribe(self, adapter_id: str, session_id: str) -> AsyncIterator[RenderedMessage]:
        raise NotImplementedError("subscribe is not used by the CLI flow in v1")

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        self._cancel_flags[session_id] = True
        for agent in self.agents.values():
            await agent.cancel(session_id, run_id)

    def is_cancel_requested(self, session_id: str) -> bool:
        return self._cancel_flags.get(session_id, False)

    def clear_cancel(self, session_id: str) -> None:
        self._cancel_flags.pop(session_id, None)

    @staticmethod
    def _attach_event_kind(rendered: RenderedMessage, event: AgentEvent) -> RenderedMessage:
        metadata = dict(rendered.metadata)
        metadata.setdefault("event_kind", event.kind)
        return replace(rendered, metadata=metadata)
