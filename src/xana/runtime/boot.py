"""boot(): wire up the first core flow in one place.

Returns a BootResult holding the AgentGateway and all the registries
under it, so the CLI and tests can introspect.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.interfaces import Agent
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
    MemorySearchEngine,
    MemoryTree,
)
from xana.memory.markdown import (
    MarkdownMemoryTree,
    SQLiteMemorySearchEngine,
    SearchMemoryInjector,
)
from xana.skills.builtin.identity import IdentitySkill
from xana.skills.interfaces import SkillRegistry
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.state.interfaces import StateRepository
from xana.tools.builtin.memory_search import MemorySearchTool
from xana.tools.interfaces import ToolRegistry
from xana.tools.registry import InMemoryToolRegistry


@dataclass
class BootResult:
    gateway: DefaultAgentGateway
    agents: dict[str, Agent]
    memory_tree: MemoryTree
    search_engine: MemorySearchEngine
    active_context_store: ActiveContextStore
    history_mirror: HistoryMirror
    memory_injector_registry: MemoryInjectorRegistry
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    model_registry: ModelRegistry
    state_repository: StateRepository


async def boot(
    *,
    project_root: Path,
    model: str = "fake-model",
    provider: str = "fake",
    scripted_reply: str = "Hello from xana.",
) -> BootResult:
    project_root = Path(project_root).resolve()

    tree = MarkdownMemoryTree(root=project_root, entrypoint="README_ai.md")
    await tree.load()
    engine = SQLiteMemorySearchEngine(":memory:")
    await engine.rebuild_from_tree(tree)
    injector = SearchMemoryInjector(engine, top_k=3, budget_chars=2000)
    injector_registry = DefaultMemoryInjectorRegistry()
    await injector_registry.register(injector, priority=100)

    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()

    tools = InMemoryToolRegistry()
    await tools.register(MemorySearchTool(engine))

    skills = InMemorySkillRegistry()
    await skills.register(IdentitySkill())

    model_registry = InMemoryModelRegistry()
    await model_registry.register(
        provider,
        FakeModelClient(
            ScriptedTurn(
                text_deltas=(scripted_reply,),
                tool_call=None,
                usage_input_tokens=4,
                usage_output_tokens=8,
            )
        ),
    )

    state = InMemoryStateRepository()

    chat_agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(),
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=state,
        cwd=str(project_root),
        model=model,
        provider=provider,
    )

    gateway = DefaultAgentGateway(
        agents={"chat": chat_agent},
        router=DefaultAgentRequestRouter(default_agent="chat"),
    )

    return BootResult(
        gateway=gateway,
        agents={"chat": chat_agent},
        memory_tree=tree,
        search_engine=engine,
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=state,
    )
