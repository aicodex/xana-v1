"""ChatAgent: default agent that calls DefaultHarness with mapped request/context."""
from __future__ import annotations

from typing import Any, AsyncGenerator, AsyncIterator

from xana.agents.contracts import (
    AgentControlRequest,
    AgentEvent,
    AgentEventKind,
    AgentRequest,
    AgentSpec,
)
from xana.agents.interfaces import (
    Agent,
    AgentEventMapper,
    AgentRequestMapper,
)
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.contracts import AbortSignal, HarnessContext, HarnessEvent, HarnessRequest
from xana.harness_sdk.interfaces import Harness, ProgressSink
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
)
from xana.policy.contracts import PermissionContext
from xana.session_config.interfaces import SessionConfigStore
from xana.skills.interfaces import SkillRegistry
from xana.state.interfaces import StateRepository
from xana.tools.interfaces import ToolRegistry


def build_chat_agent_spec() -> AgentSpec:
    return AgentSpec(
        component=ComponentSpec(
            kind="agent",
            name="chat",
            qualified_name="builtin.chat",
            description="Default chat agent.",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        prompt_id="chat-default",
    )


class DefaultAgentRequestMapper(AgentRequestMapper):
    def __init__(
        self,
        *,
        active_context_store: ActiveContextStore,
        history_mirror: HistoryMirror,
        memory_injector_registry: MemoryInjectorRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
        session_config_store: SessionConfigStore | None = None,
        progress_sink: ProgressSink | None = None,
        context_window: int | None = None,
        force_compact: int | None = None,
        model_thresholds: dict[str, tuple[int, int]] | None = None,
        main_writer: Any = None,
        run_jsonl_writer: Any = None,
        write_queue: Any = None,
        todo_store: Any = None,
        budget_tracker: Any = None,
        episode_runner: Any = None,
    ) -> None:
        self.context_window = context_window
        self.force_compact = force_compact
        self.model_thresholds = model_thresholds
        self.main_writer = main_writer
        self.run_jsonl_writer = run_jsonl_writer
        self.write_queue = write_queue
        self.todo_store = todo_store
        self.budget_tracker = budget_tracker
        self.episode_runner = episode_runner
        self.active_context_store = active_context_store
        self.history_mirror = history_mirror
        self.memory_injector_registry = memory_injector_registry
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.model_registry = model_registry
        self.state_repository = state_repository
        self.cwd = cwd
        self.model = model
        self.provider = provider
        self.session_config_store = session_config_store
        self.progress_sink = progress_sink
        self._run_counter = 0

    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest:
        return HarnessRequest(
            session_id=request.session_id,
            agent_name=spec.component.name,
            user_input=request.text,
            metadata=dict(request.metadata),
        )

    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext:
        import uuid
        self._run_counter += 1
        # gateway 把它生成的 run_id 通过 inbound.metadata → request.metadata 传进来,
        # 这样 adapter 端 _render_states 的 key 和 harness 事件的 run_id 对上。
        # 没有就 fallback uuid(单测、命令行直接调用 ChatAgent 的场景)。
        run_id = str(request.metadata.get("gateway_run_id") or
                     f"{request.session_id}-{uuid.uuid4().hex[:8]}")
        model = self.model
        effort: str | int | None = None
        async_mode = True  # P3 完成,默认 async
        cfg = None
        if self.session_config_store is not None:
            cfg = await self.session_config_store.get(request.session_id)
            if cfg.model:
                model = cfg.model
            if cfg.effort is not None:
                effort = cfg.effort
            async_mode = cfg.async_mode
        ctx_win, force_at = self.context_window, self.force_compact
        if self.model_thresholds and model in self.model_thresholds:
            ctx_win, force_at = self.model_thresholds[model]
        config_snapshot: dict[str, Any] = {
            "provider": self.provider,
            "context_window": ctx_win,
            "force_compact": force_at,
            "agent_identity": spec.identity,
            "agent_system_prompt": spec.system_prompt,
            "async_mode": async_mode,
            "max_turns": spec.max_turns,
        }
        if cfg is not None:
            config_snapshot.update({
                "streaming": cfg.streaming,
                "thinking": cfg.thinking,
                "search_provider": cfg.search_provider,
            })
        metadata: dict[str, Any] = {}
        if self.main_writer is not None:
            metadata["main_writer"] = self.main_writer
        if self.run_jsonl_writer is not None:
            metadata["run_jsonl_writer"] = self.run_jsonl_writer
        if self.write_queue is not None:
            metadata["write_queue"] = self.write_queue
            # Resolve sessions_root from run_jsonl_writer so the harness can
            # load runs/{run_id}.jsonl to build ReplaceItem on finalize.
            jw = self.run_jsonl_writer
            if jw is not None:
                runs_dir = getattr(jw, "_dir", None)
                if runs_dir is not None:
                    metadata["runs_dir"] = runs_dir
        if self.todo_store is not None:
            # todo_store:harness 据此在长时间未更新 todo 时追加 todo_reminder note。
            metadata["todo_store"] = self.todo_store
        if self.budget_tracker is not None:
            # §4.3: 预算护栏 concrete helper,在 update_usage 写点累计/检查。
            metadata["budget_tracker"] = self.budget_tracker
        if self.episode_runner is not None:
            # §6.2: episode 状态机,软/硬阈值在 usage 写点消费。
            metadata["episode_runner"] = self.episode_runner
        # P5 fix — gateway owns AbortSignal lifecycle. If it pre-registered
        # one for this run, reuse the same instance so gateway.cancel() flips
        # the bool the harness's tool-abort poll is watching. Fall back to a
        # fresh instance for tests / direct ChatAgent invocations.
        abort_signal = request.metadata.get("abort_signal")
        if not isinstance(abort_signal, AbortSignal):
            abort_signal = AbortSignal()
        # EnterPlanMode 持久化的 mode="plan" 标记在这里真正进入权限上下文,
        # 由 harness 的工具权限检查点消费(非只读工具在 plan 模式被拒)。
        permission_mode = "default"
        if cfg is not None and cfg.metadata.get("mode") == "plan":
            permission_mode = "plan"
        return HarnessContext(
            session_id=request.session_id,
            agent_name=spec.component.name,
            cwd=self.cwd,
            model=model,
            effort=effort,
            run_id=run_id,
            permission=PermissionContext(mode=permission_mode, session_id=request.session_id),
            abort=abort_signal,
            state_repository=self.state_repository,
            active_context_store=self.active_context_store,
            history_mirror=self.history_mirror,
            memory_injector_registry=self.memory_injector_registry,
            tool_registry=self.tool_registry,
            skill_registry=self.skill_registry,
            model_registry=self.model_registry,
            config_snapshot=config_snapshot,
            progress_sink=self.progress_sink,
            async_mode=async_mode,
            metadata=metadata,
        )


_HARNESS_TO_AGENT_KIND: dict[str, AgentEventKind] = {
    "final": "final",
    "error": "error",
    "tool_finished": "tool",
    "cancelled": "cancelled",
}


class DefaultAgentEventMapper(AgentEventMapper):
    async def map(self, session_id: str, event: HarnessEvent) -> AgentEvent:
        kind = _HARNESS_TO_AGENT_KIND.get(event.kind, "draft")
        return AgentEvent(
            kind=kind,
            session_id=session_id,
            payload=dict(event.payload),
            harness_event=event,
        )


class ChatAgent(Agent):
    def __init__(
        self,
        *,
        spec: AgentSpec,
        harness: Harness,
        active_context_store: ActiveContextStore,
        history_mirror: HistoryMirror,
        memory_injector_registry: MemoryInjectorRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
        session_config_store: SessionConfigStore | None = None,
        progress_sink: ProgressSink | None = None,
        context_window: int | None = None,
        force_compact: int | None = None,
        model_thresholds: dict[str, tuple[int, int]] | None = None,
        main_writer: Any = None,
        run_jsonl_writer: Any = None,
        write_queue: Any = None,
        todo_store: Any = None,
        budget_tracker: Any = None,
        episode_runner: Any = None,
    ) -> None:
        self._spec = spec
        self.harness = harness
        self.progress_sink = progress_sink
        self.request_mapper = DefaultAgentRequestMapper(
            active_context_store=active_context_store,
            history_mirror=history_mirror,
            memory_injector_registry=memory_injector_registry,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            model_registry=model_registry,
            state_repository=state_repository,
            cwd=cwd,
            model=model,
            provider=provider,
            session_config_store=session_config_store,
            progress_sink=progress_sink,
            context_window=context_window,
            force_compact=force_compact,
            model_thresholds=model_thresholds,
            main_writer=main_writer,
            run_jsonl_writer=run_jsonl_writer,
            write_queue=write_queue,
            todo_store=todo_store,
            budget_tracker=budget_tracker,
            episode_runner=episode_runner,
        )
        self.event_mapper = DefaultAgentEventMapper()
        self._pending_cancel_sessions: set[str] = set()

    def spec(self) -> AgentSpec:
        return self._spec

    def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        return self._send(request)

    async def _send(self, request: AgentRequest) -> AsyncGenerator[AgentEvent, None]:
        harness_request = await self.request_mapper.to_harness_request(self._spec, request)
        harness_context = await self.request_mapper.to_harness_context(self._spec, request)
        if request.session_id in self._pending_cancel_sessions:
            # Next-turn semantics: a cancel arrived between turns. Trigger
            # the live abort signal so the new turn aborts at the first
            # poll-point. AbortSignal is now mutable (P5 fix), so we flip
            # in-place rather than swapping in a new frozen instance.
            harness_context.abort.trigger("user_cancel")
            self._pending_cancel_sessions.discard(request.session_id)
        async for harness_event in self.harness.invoke(harness_request, harness_context):
            yield await self.event_mapper.map(request.session_id, harness_event)

    def control(self, request: AgentControlRequest) -> AsyncIterator[AgentEvent]:
        return self._control(request)

    async def _control(self, request: AgentControlRequest) -> AsyncGenerator[AgentEvent, None]:
        yield AgentEvent(
            kind="control_result",
            session_id=request.session_id,
            payload={"op": request.op, "ok": False, "reason": "not implemented"},
        )

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        # Only mark a between-turn pending cancel when no specific run_id was
        # given. When the gateway knows the run, it has already flipped the
        # live AbortSignal for the in-flight turn — adding to the pending set
        # would cause the NEXT user submit to immediately abort at the
        # harness's first poll (yielding nothing visible to the user).
        if run_id is None:
            self._pending_cancel_sessions.add(session_id)
