"""boot(): wire up the first core flow in one place.

Returns a BootResult holding the AgentGateway and all the registries
under it, so the CLI and tests can introspect.

Model selection happens here: the DefaultConfigProvider reads
~/.xana/config/system.json (or $XANA_HOME), picks the chat default model,
and we register the matching client (AnthropicDeepSeekClient for deepseek
entries, FakeModelClient as the fallback when no api_key is configured).
ChatAgent's provider/model are pinned to whatever was actually registered
so config_snapshot["provider"] resolves cleanly inside the harness.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.progress import AdapterProgressSink
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.interfaces import Agent
from xana.config.loader import DefaultConfigProvider, derive_compact_thresholds
from xana.extension_api.registry import ExtensionRegistry
from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.interfaces import ProgressSink
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.llm_driver.interfaces import ModelClient, ModelRegistry
from xana.llm_driver.providers.anthropic_deepseek import (
    AnthropicDeepSeekClient,
    AnthropicDeepSeekConfig,
)
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
from xana.skills.interfaces import SkillRegistry
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.state.interfaces import StateRepository
from xana.session_config.file_store import FileSessionConfigStore
from xana.session_config.interfaces import SessionConfigStore
from xana.tools.builtin._todo_store import TodoStore
from xana.tools.builtin.bash import BashTool
from xana.tools.builtin.edit import EditTool
from xana.tools.builtin.enter_plan_mode import EnterPlanModeTool
from xana.tools.builtin.enter_worktree import EnterWorktreeTool
from xana.tools.builtin.glob_tool import GlobTool
from xana.tools.builtin.grep_tool import GrepTool
from xana.tools.builtin.memory_search import MemorySearchTool
from xana.tools.builtin.read import ReadTool
from xana.tools.builtin.todo_write import TodoWriteTool
from xana.tools.builtin.tool_search import ToolSearchTool
from xana.tools.builtin.write import WriteTool
from xana.tools.interfaces import ToolRegistry
from xana.tools.registry import InMemoryToolRegistry


_INJECTOR_TOP_K = 3
_INJECTOR_BUDGET_CHARS = 2000
_FAKE_PROVIDER = "fake"


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
    session_config_store: SessionConfigStore
    progress_sink: ProgressSink
    provider: str
    model: str


async def boot(
    *,
    project_root: Path,
    config_provider: DefaultConfigProvider | None = None,
    extension_registry: ExtensionRegistry | None = None,
) -> BootResult:
    project_root = Path(project_root).resolve()

    tree = MarkdownMemoryTree(root=project_root, entrypoint="README_ai.md")
    await tree.load()
    engine = SQLiteMemorySearchEngine(":memory:")
    await engine.rebuild_from_tree(tree)
    injector = SearchMemoryInjector(engine, top_k=_INJECTOR_TOP_K, budget_chars=_INJECTOR_BUDGET_CHARS)
    injector_registry = DefaultMemoryInjectorRegistry()
    await injector_registry.register(injector, priority=100)

    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()

    tools = InMemoryToolRegistry()
    await tools.register(MemorySearchTool(engine))
    await tools.register(ReadTool())
    await tools.register(EditTool())
    await tools.register(WriteTool())
    await tools.register(GlobTool())
    await tools.register(GrepTool())
    await tools.register(BashTool())
    await tools.register(ToolSearchTool(tool_registry=tools))

    todo_root = Path.home() / ".xana" / "sessions"
    worktree_root = Path.home() / ".xana" / "worktrees"
    session_config_root = Path.home() / ".xana" / "sessions"
    session_config_store: SessionConfigStore = FileSessionConfigStore(root=session_config_root)
    await tools.register(TodoWriteTool(TodoStore(root=todo_root)))
    await tools.register(EnterPlanModeTool(session_config_store=session_config_store))
    await tools.register(EnterWorktreeTool(session_config_store=session_config_store, worktree_root=worktree_root))

    skills = InMemorySkillRegistry()

    cp = config_provider or DefaultConfigProvider()
    runtime_config = await cp.load(project_root)
    system_cfg = cp.load_system_config()
    model_thresholds = {e.name: derive_compact_thresholds(e) for e in system_cfg.models}
    provider = runtime_config.default_model.provider
    model = runtime_config.default_model.model
    client = _build_model_client(provider, model, cp)
    model_registry = InMemoryModelRegistry()
    await model_registry.register(provider, client)

    state = InMemoryStateRepository()

    gateway = DefaultAgentGateway(
        agents={},
        router=DefaultAgentRequestRouter(default_agent="chat"),
    )

    progress_sink = AdapterProgressSink(gateway.adapters)

    chat_agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(extension_registry=extension_registry),
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
        session_config_store=session_config_store,
        progress_sink=progress_sink,
        context_window=runtime_config.default_model.context_window,
        force_compact=runtime_config.default_model.force_compact,
        model_thresholds=model_thresholds,
    )

    gateway.agents["chat"] = chat_agent

    command_router = DefaultCommandRouter(
        session_config_store=session_config_store,
        active_context_store=active_ctx,
        gateway=gateway,
        available_models=tuple(runtime_config.models.keys()) if hasattr(runtime_config, "models") else (model,),
        available_search_providers=("bm25",),
        model_registry=model_registry,
        provider=provider,
        model=model,
        progress_sink=progress_sink,
    )
    await gateway.register_command_router(command_router)

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
        session_config_store=session_config_store,
        progress_sink=progress_sink,
        provider=provider,
        model=model,
    )


def _build_model_client(provider: str, model: str, cp: DefaultConfigProvider) -> ModelClient:
    """Pick a concrete ModelClient based on the resolved provider string.

    Treats any provider starting with "deepseek" as the DSv4 Anthropic-dialect
    client. The model entry's api_key + optional base_url are pulled from the
    SystemConfig that DefaultConfigProvider cached during load(). Missing
    entry or empty api_key falls back to FakeModelClient so a fresh setup
    (empty models[]) still boots.
    """
    if provider == _FAKE_PROVIDER:
        return _fake()
    if provider.startswith("deepseek"):
        system = cp.load_system_config()
        entry = system.model(model)
        if entry is None or not entry.api_key:
            return _fake()
        config = (
            AnthropicDeepSeekConfig(api_key=entry.api_key, base_url=entry.base_url)
            if entry.base_url
            else AnthropicDeepSeekConfig(api_key=entry.api_key)
        )
        return AnthropicDeepSeekClient(config)
    return _fake()


def _fake() -> FakeModelClient:
    return FakeModelClient(ScriptedTurn(usage_input_tokens=4, usage_output_tokens=8))
