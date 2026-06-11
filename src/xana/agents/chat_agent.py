"""ChatAgent: default agent that calls DefaultHarness with mapped request/context."""
from __future__ import annotations

from typing import AsyncGenerator, AsyncIterator

from xana.agents.contracts import (
    AgentControlRequest,
    AgentEvent,
    AgentRequest,
    AgentSpec,
)
from xana.agents.interfaces import (
    Agent,
    AgentEventMapper,
    AgentRequestMapper,
)
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest
from xana.harness_sdk.interfaces import Harness
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
)
from xana.policy.contracts import PermissionContext
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
    ) -> None:
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
        self._run_counter = 0

    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest:
        return HarnessRequest(
            session_id=request.session_id,
            agent_name=spec.component.name,
            user_input=request.text,
            metadata=dict(request.metadata),
        )

    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext:
        self._run_counter += 1
        run_id = f"{request.session_id}-{self._run_counter}"
        return HarnessContext(
            session_id=request.session_id,
            agent_name=spec.component.name,
            cwd=self.cwd,
            model=self.model,
            run_id=run_id,
            permission=PermissionContext(mode="default", session_id=request.session_id),
            state_repository=self.state_repository,
            active_context_store=self.active_context_store,
            history_mirror=self.history_mirror,
            memory_injector_registry=self.memory_injector_registry,
            tool_registry=self.tool_registry,
            skill_registry=self.skill_registry,
            model_registry=self.model_registry,
            config_snapshot={"provider": self.provider},
        )


class DefaultAgentEventMapper(AgentEventMapper):
    async def map(self, session_id: str, event: HarnessEvent) -> AgentEvent:
        if event.kind == "final":
            kind = "final"
        elif event.kind == "error":
            kind = "error"
        else:
            kind = "draft"
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
    ) -> None:
        self._spec = spec
        self.harness = harness
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
        )
        self.event_mapper = DefaultAgentEventMapper()

    def spec(self) -> AgentSpec:
        return self._spec

    def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        return self._send(request)

    async def _send(self, request: AgentRequest) -> AsyncGenerator[AgentEvent, None]:
        harness_request = await self.request_mapper.to_harness_request(self._spec, request)
        harness_context = await self.request_mapper.to_harness_context(self._spec, request)
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
        return None
