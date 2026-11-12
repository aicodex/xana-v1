"""ChatAgent: default agent that calls DefaultHarness with mapped request/context."""
from __future__ import annotations

from dataclasses import replace
import asyncio
from contextlib import aclosing
from xana.multimodal.files import FileMediaStore, MAX_ATTACHMENTS
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator

from xana.config.system_model import AgentDefaults, ModelEntry
from xana.config.loader import resolve_model_config
from xana.config.contracts import ModelConfig
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
from xana.harness_sdk.contracts import (
    AbortSignal,
    HarnessContext,
    HarnessEvent,
    HarnessRequest,
    PromptProfile,
)
from xana.harness_sdk.interfaces import Harness, ProgressSink
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
)
from xana.policy.contracts import PermissionContext
from xana.session_config.interfaces import SessionConfigStore
from xana.state.interfaces import StateRepository
from xana.tools.interfaces import ToolRegistry


def build_chat_agent_spec(
    defaults: AgentDefaults | None = None,
    *,
    extra_tools: tuple[str, ...] = (),
) -> AgentSpec:
    """内置 chat;工具 / 技能按项目默认(docs/design/project-design.md §8)。

    extra_tools 用于项目能力开关(如接了飞书机器人时加 media 组),不影响自建 agent。
    """
    defaults = defaults or AgentDefaults()
    tools = tuple(dict.fromkeys((*defaults.tools, *extra_tools)))
    return AgentSpec(
        component=ComponentSpec(
            kind="agent",
            name="chat",
            qualified_name="builtin.chat",
            description="默认聊天助手",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        prompt_id="chat-default",
        tools=tools,
        disallowed_tools=tuple(defaults.disallowed_tools),
        skills=tuple(defaults.skills) if defaults.skills is not None else None,
    )


class DefaultAgentRequestMapper(AgentRequestMapper):
    def __init__(
        self,
        *,
        active_context_store: ActiveContextStore,
        history_mirror: HistoryMirror,
        memory_injector_registry: MemoryInjectorRegistry,
        tool_registry: ToolRegistry,
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
        media_store: FileMediaStore | None,
        session_config_store: SessionConfigStore | None = None,
        progress_sink: ProgressSink | None = None,
        model_configs: dict[str, ModelConfig] | None = None,
        run_jsonl_writer: Any = None,
        budget_tracker: Any = None,
        episode_runner: Any = None,
    ) -> None:
        self.model_configs = dict(model_configs) if model_configs is not None else None
        self.run_jsonl_writer = run_jsonl_writer
        self.budget_tracker = budget_tracker
        self.episode_runner = episode_runner
        self.active_context_store = active_context_store
        self.history_mirror = history_mirror
        self.memory_injector_registry = memory_injector_registry
        self.tool_registry = tool_registry
        self.model_registry = model_registry
        self.state_repository = state_repository
        self.cwd = cwd
        self.model = model
        self.provider = provider
        self.media_store = media_store
        self.session_config_store = session_config_store
        self.progress_sink = progress_sink

    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest:
        media = []
        if request.attachments:
            if self.media_store is None:
                raise ValueError("媒体输入存储未配置")
            if len(request.attachments) > MAX_ATTACHMENTS:
                raise ValueError(f"单条请求最多支持 {MAX_ATTACHMENTS} 个媒体附件")
            for attachment in request.attachments:
                path = Path(attachment["file_path"])
                if not path.is_absolute():
                    path = Path(self.cwd) / path
                media.append(await asyncio.to_thread(self.media_store.import_file, path))
        return HarnessRequest(
            session_id=request.session_id,
            agent_name=spec.component.name,
            user_input=request.text,
            media=tuple(media),
            metadata={**request.metadata, "origin": request.source},
        )

    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext:
        import uuid
        # gateway 把它生成的 run_id 通过 inbound.metadata → request.metadata 传进来,
        # 这样 adapter 端卡片状态的 key 和 harness 事件的 run_id 对上。
        # 没有就 fallback uuid(单测、命令行直接调用 ChatAgent 的场景)。
        run_id = str(request.metadata.get("gateway_run_id") or
                     f"run-{uuid.uuid4().hex[:24]}")
        # 模型 / 思考强度:会话设置(/model、/effort)> agent 自己的设置(AGENT.md)> 缺省
        model = spec.model or self.model
        effort: str | int | None = spec.effort
        cfg = None
        profile = spec.prompt_profile or PromptProfile()
        if self.session_config_store is not None:
            cfg = await self.session_config_store.get(request.session_id)
            if cfg.permission_mode is None and request.source == "feishu":
                cfg = replace(cfg, permission_mode="auto")
                await self.session_config_store.save(cfg)
            if cfg.model:
                model = cfg.model
            if cfg.effort is not None:
                effort = cfg.effort
            # PromptProfile 在会话首次使用时固化,之后改 AGENT.md 不影响已有会话(system 会话内不变)
            if cfg.prompt_profile is None:
                cfg = replace(cfg, prompt_profile=profile.to_dict())
                await self.session_config_store.save(cfg)
            else:
                profile = PromptProfile.from_dict(cfg.prompt_profile)
        model_config = self.model_configs.get(model) if self.model_configs is not None else None
        if model_config is None:
            model_config = resolve_model_config(ModelEntry(name=model, type=self.provider))
        config_snapshot: dict[str, Any] = {
            "provider": self.provider,
            "context_window": model_config.context_window,
            "force_compact": model_config.force_compact,
            "blocking_limit": model_config.blocking_limit,
            "summary_max_tokens": model_config.summary_max_tokens,
            "max_tokens": model_config.max_tokens,
            "cache_ttl_seconds": model_config.cache_ttl_seconds,
            "agent_system_prompt": spec.system_prompt,
            "max_turns": spec.max_turns,
            "prompt_profile": profile.to_dict(),
            "verify": [c.to_dict() for c in spec.verify],
            "tools": list(spec.tools),
            "disallowed_tools": list(spec.disallowed_tools),
            "tool_ceiling": sorted(request.tool_ceiling) if request.tool_ceiling is not None else None,
            "skills": (None if spec.skills is None or "*" in spec.skills else sorted(spec.skills)),
        }
        if cfg is not None:
            config_snapshot.update({
                "streaming": cfg.streaming,
                "thinking": cfg.thinking,
                "search_provider": cfg.search_provider,
                "view_mode": cfg.view_mode,
            })
        metadata: dict[str, Any] = {}
        if self.budget_tracker is not None:
            # §4.3: 预算护栏 concrete helper,在 update_usage 写点累计/检查。
            metadata["budget_tracker"] = self.budget_tracker
        if self.episode_runner is not None:
            # §6.2: episode 状态机,软/硬阈值在 usage 写点消费。
            metadata["episode_runner"] = self.episode_runner
        permission_mode = (cfg.permission_mode if cfg else None) or "yolo"
        cwd = self.cwd
        if cfg is not None and cfg.metadata.get("worktree_path"):
            worktree = Path(cfg.metadata["worktree_path"])
            if not worktree.is_dir():
                raise FileNotFoundError(f"会话工作树目录不存在: {worktree}")
            cwd = str(worktree.resolve())
        return HarnessContext(
            session_id=request.session_id,
            agent_name=spec.component.name,
            cwd=cwd,
            model=model,
            effort=effort,
            run_id=run_id,
            permission=PermissionContext(mode=permission_mode, session_id=request.session_id,
                                         plan_mode=bool(cfg and cfg.metadata.get("mode") == "plan")),
            abort=AbortSignal(),
            state_repository=self.state_repository,
            active_context_store=self.active_context_store,
            history_mirror=self.history_mirror,
            memory_injector_registry=self.memory_injector_registry,
            tool_registry=self.tool_registry,
            model_registry=self.model_registry,
            config_snapshot=config_snapshot,
            progress_sink=self.progress_sink,
            metadata=metadata,
            run_writer=self.run_jsonl_writer,
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
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
        media_store: FileMediaStore | None,
        session_config_store: SessionConfigStore | None = None,
        progress_sink: ProgressSink | None = None,
        model_configs: dict[str, ModelConfig] | None = None,
        run_jsonl_writer: Any = None,
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
            model_registry=model_registry,
            state_repository=state_repository,
            cwd=cwd,
            model=model,
            provider=provider,
            media_store=media_store,
            session_config_store=session_config_store,
            progress_sink=progress_sink,
            model_configs=model_configs,
            run_jsonl_writer=run_jsonl_writer,
            budget_tracker=budget_tracker,
            episode_runner=episode_runner,
        )
        self.event_mapper = DefaultAgentEventMapper()

    def spec(self) -> AgentSpec:
        return self._spec

    def send(self, request: AgentRequest) -> AsyncGenerator[AgentEvent, None]:
        return self._send(request)

    async def _send(self, request: AgentRequest) -> AsyncGenerator[AgentEvent, None]:
        harness_request = await self.request_mapper.to_harness_request(self._spec, request)
        harness_context = await self.request_mapper.to_harness_context(self._spec, request)
        async with aclosing(self.harness.invoke(harness_request, harness_context)) as events:
            async for harness_event in events:
                yield await self.event_mapper.map(request.session_id, harness_event)

    def control(self, request: AgentControlRequest) -> AsyncGenerator[AgentEvent, None]:
        return self._control(request)

    async def _control(self, request: AgentControlRequest) -> AsyncGenerator[AgentEvent, None]:
        yield AgentEvent(
            kind="control_result",
            session_id=request.session_id,
            payload={"op": request.op, "ok": False, "reason": "not implemented"},
        )

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        await self.harness.cancel(session_id, run_id)
