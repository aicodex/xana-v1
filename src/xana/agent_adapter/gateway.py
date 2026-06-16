"""DefaultAgentGateway: wires Adapter <-> Agent."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, AsyncGenerator, AsyncIterator

logger = logging.getLogger(__name__)

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
from xana.harness_sdk.contracts import AbortSignal


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
        # P5 fix — live abort signal per run_id. cancel() flips this in-place
        # so an already-running turn's tool poll loop sees it. Cleaned up in
        # _submit finally.
        self._abort_signals: dict[str, AbortSignal] = {}
        self._cmd_seq = 0
        self._inflight: dict[str, list[InflightRun]] = {}  # session_id → 倒序(新在前)
        self._run_seq = 0
        # P5 — RunStore + RunJsonlWriter injected post-construction (boot wiring).
        self.run_store: Any = None
        self.run_jsonl_writer: Any = None
        self._resume_inflight: set[str] = set()

    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None:
        self.adapters[adapter_id] = adapter

    async def register_command_router(self, router: CommandRouter) -> None:
        self.command_router = router

    async def register_binding(self, binding: SessionBinding) -> None:
        self.bindings[binding.session_id] = binding

    async def detach_foreground(self, session_id: str) -> None:
        for ir in self._inflight.get(session_id, []):
            if ir.is_attached:
                ir.is_attached = False
                for adapter in self.adapters.values():
                    self._notify_run_detached(adapter, session_id, ir)
                return

    async def attach(self, session_id: str, run_id: str) -> None:
        runs = self._inflight.get(session_id, [])
        target = next((ir for ir in runs if ir.run_id == run_id), None)
        if target is None:
            return  # unknown / already replaced; noop per spec
        # auto-detach current fg(s)
        for ir in runs:
            if ir.run_id != run_id and ir.is_attached:
                ir.is_attached = False
                for ad in self.adapters.values():
                    self._notify_run_detached(ad, session_id, ir)
        target.is_attached = True
        for ad in self.adapters.values():
            note = getattr(ad, "note_progress", None)
            if note is not None:
                note({
                    "kind": "attached",
                    "session_id": session_id,
                    "run_id": run_id,
                    "label": target.label,
                })

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
            # /compact 也跳过预建 — 当有 in-flight 时它要挂在 PH 卡前,
            # 自建命令卡会导致 spec §3 违反(同时有 cmd 卡 + PH 卡)。
            text_head = inbound.text.strip().split(" ", 1)[0]
            silent_cmd = text_head in ("/compact_cancel", "/compact")
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
                # /attach 类命令:渲染菜单前先把当前 fg detach,
                # 用户点击菜单按钮后 gateway.attach 再切回新目标。
                if cmd_result.payload.get("requires_detach"):
                    await self.detach_foreground(inbound.session_id)
                event = AgentEvent(
                    kind="final" if cmd_result.ok else "error",
                    session_id=inbound.session_id,
                    payload={"text": cmd_result.message, "run_id": run_id, **cmd_result.payload},
                )
                rendered = await adapter.render(event)
                yield self._attach_event_kind(rendered, event)
                return

        # 旧 run 转后台 —— 不 cancel,只标 detached,并通知所有 adapter 刷 tab。
        await self.detach_foreground(inbound.session_id)

        import uuid as _uuid
        self._run_seq += 1
        # seq + uuid 后缀:seq 保证可读顺序,uuid 避免重启后 RunRecord 文件名冲突。
        new_run = InflightRun(
            session_id=inbound.session_id,
            run_id=f"run-{inbound.session_id}-{self._run_seq}-{_uuid.uuid4().hex[:8]}",
            label=(inbound.text or "").strip()[:40] or "run",
        )
        self._inflight.setdefault(inbound.session_id, []).insert(0, new_run)
        # P5 fix — register a live AbortSignal for this run BEFORE handing off
        # to the agent. cancel(run_id) flips this same instance in-place so
        # the harness's tool-abort poll sees it on the currently-running turn.
        abort_signal = AbortSignal()
        self._abort_signals[new_run.run_id] = abort_signal
        # gateway 的 run_id 必须传给 harness,否则 adapter 端 _render_states 用
        # gateway run_id 建 key,但 render() 收到的事件 run_id 是 harness 自产的
        # uuid 形式,frozen 检查永远 miss → detach 后旧卡还在刷流。
        inbound = replace(
            inbound, metadata={
                **inbound.metadata,
                "gateway_run_id": new_run.run_id,
                "abort_signal": abort_signal,
            },
        )
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
        # PH 状态机依赖 run_started 才能在 adapter 端初始化 RunRenderState;
        # 不发的话 _render_states 永远空,detach/freeze/switch 全部失效。
        note = getattr(adapter, "note_progress", None)
        if note is not None:
            note({
                "kind": "run_started",
                "session_id": inbound.session_id,
                "run_id": new_run.run_id,
                "label": new_run.label,
            })
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
            # 先看 abort 是否被触发(决定 ph_replaced 该不该带 cancelled 标记),
            # 再 pop 信号 — pop 顺序反了会丢标记。
            was_cancelled = bool(abort_signal.aborted)
            self._abort_signals.pop(new_run.run_id, None)
            note = getattr(adapter, "note_progress", None)
            if note is not None:
                note({
                    "kind": "ph_replaced",
                    "session_id": inbound.session_id,
                    "run_id": new_run.run_id,
                    "mode": "full",
                    "cancelled": was_cancelled,
                })
                if not new_run.is_attached:
                    # 这个 run 在跑的过程中被新的 submit detach 掉了 → 完成时通知 adapter
                    note({
                        "kind": "run_completed_bg",
                        "session_id": inbound.session_id,
                        "run_id": new_run.run_id,
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
        # P5 fix — flip the live AbortSignal so an in-flight tool's poll loop
        # in DefaultHarness._run_tool_with_abort sees abort.aborted == True.
        # Without this, cancel only took effect on the NEXT turn (via
        # ChatAgent._pending_cancel_sessions), so e.g. SlowWrite ran to
        # completion despite the stop click.
        if run_id is not None:
            signal = self._abort_signals.get(run_id)
            if signal is not None:
                signal.trigger("user_cancel")
        else:
            # Whole-session cancel — trigger every signal for this session's
            # inflight runs (cancel_all path or run_id-less callers).
            for ir in self._inflight.get(session_id, []):
                signal = self._abort_signals.get(ir.run_id)
                if signal is not None:
                    signal.trigger("user_cancel")
        for agent in self.agents.values():
            await agent.cancel(session_id, run_id)
        # P5: mark exit_reason=user_cancel so list_unfinished/RunRecord reflect
        # the cancel source. cancel_all overwrites this with shutdown_graceful
        # by calling update_status again with the shutdown reason.
        if self.run_store is not None and run_id is not None:
            try:
                rec = await self.run_store.get(run_id)
                if rec is not None:
                    await self.run_store.update_status(
                        run_id, rec.status,
                        fields={"exit_reason": "user_cancel"},
                    )
            except Exception:
                pass

    async def resume(self, session_id: str, run_id: str) -> None:
        """续跑 = (1) 把原 run JSONL 改名 .aborted.jsonl
                 (2) 用原 input_request fork 新 run。

        防重入:同一 run_id 同时只能 resume 一次。
        """
        if run_id in self._resume_inflight:
            return
        if not self.adapters:
            logger.warning(
                "resume failed: no adapters registered for run %s", run_id,
            )
            return
        self._resume_inflight.add(run_id)
        try:
            rec = await self.run_store.get(run_id) if self.run_store is not None else None
            if rec is None:
                return
            if self.run_jsonl_writer is not None:
                self.run_jsonl_writer.rename_to_aborted(run_id)
            text = rec.input_request.get("text", "") if rec.input_request else ""
            # Fork a new run via the normal submit path. Adapter routing is
            # picked from any registered adapter for this session — fallback
            # to the first adapter we know about.
            adapter_id = ""
            for aid in self.adapters:
                adapter_id = aid
                break
            inbound = InboundMessage(
                adapter_id=adapter_id, session_id=session_id, text=text,
            )
            async for _ in self._submit(inbound):
                pass
        finally:
            self._resume_inflight.discard(run_id)

    async def cancel_all(self) -> None:
        """SIGTERM 入口 — 翻所有 inflight runs 的 abort,标 exit_reason=shutdown_graceful。"""
        from xana.state.contracts import RunRecord  # local to avoid boot cycles
        for sid, runs in list(self._inflight.items()):
            for ir in list(runs):
                await self.cancel(ir.session_id, ir.run_id)
                if self.run_store is None:
                    continue
                rec = await self.run_store.get(ir.run_id)
                if rec is None:
                    continue
                from dataclasses import replace as _replace
                updated = _replace(rec, exit_reason="shutdown_graceful")
                try:
                    await self.run_store.update_status(
                        ir.run_id, updated.status,
                        fields={"exit_reason": "shutdown_graceful"},
                    )
                except Exception:
                    pass

    def has_background(self) -> bool:
        for runs in self._inflight.values():
            for ir in runs:
                if ir.task is not None and not ir.task.done():
                    return True
        return False

    def is_cancel_requested(self, session_id: str) -> bool:
        return self._cancel_flags.get(session_id, False)

    def clear_cancel(self, session_id: str) -> None:
        self._cancel_flags.pop(session_id, None)

    @staticmethod
    def _attach_event_kind(rendered: RenderedMessage, event: AgentEvent) -> RenderedMessage:
        metadata = dict(rendered.metadata)
        metadata.setdefault("event_kind", event.kind)
        return replace(rendered, metadata=metadata)
