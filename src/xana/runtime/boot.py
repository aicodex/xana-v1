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

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.progress import AdapterProgressSink
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.interfaces import Agent
from xana.config.loader import (
    DefaultConfigProvider,
    default_system_json_source,
    derive_compact_thresholds,
)
from xana.extension_api.builtin.skills_listing import SkillsListingContributor
from xana.extension_api.registry import ExtensionRegistry, ExtensionRegistryBuilder
from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.interfaces import ProgressSink
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.llm_driver.interfaces import ModelClient, ModelRegistry
from xana.llm_driver.providers.anthropic_deepseek import (
    SUPPORTED_EFFORTS as DEEPSEEK_EFFORTS,
    AnthropicDeepSeekClient,
    AnthropicDeepSeekConfig,
)
from xana.llm_driver.providers.kimi import SUPPORTED_EFFORTS as KIMI_EFFORTS
from xana.llm_driver.providers.kimi import KimiClient, KimiConfig
from xana.llm_driver.providers.kimi_oauth import KimiTokenStore, api_base as kimi_api_base
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.memory.file_history import FileHistoryMirror, FileMemoryReplayer, replay_and_detect_interrupts
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
from xana.skills.bundled.loop import LoopSkill
from xana.skills.bundled.schedule import ScheduleSkill
from xana.skills.bundled.skill_creator import SkillCreatorSkill
from xana.skills.interfaces import SkillRegistry
from xana.skills.markdown_loader import MarkdownDocParser
from xana.skills.registry import InMemorySkillRegistry
from xana.memory.main_writer import MainWriter
from xana.memory.write_queue import WriteQueue, WriteQueueConsumer
from xana.state.budget import BudgetTracker
from xana.state.file_runs import FileRunStore, RunJsonlWriter
from xana.state.in_memory import InMemorySessionStore, InMemoryStateRepository
from xana.state.interfaces import RunStore, SessionStore, StateRepository
from xana.session_config.file_store import FileSessionConfigStore
from xana.session_config.interfaces import SessionConfigStore
from xana.tools.builtin._todo_store import TodoStore
from xana.runtime.scheduler import FileSchedulerService
from xana.runtime.session_resolver import SessionResolver
from xana.tools.builtin.bash import BashTool
from xana.tools.builtin.cron import CronCreateTool, CronDeleteTool, CronListTool
from xana.tools.builtin.edit import EditTool
from xana.tools.builtin.enter_plan_mode import EnterPlanModeTool
from xana.tools.builtin.enter_worktree import EnterWorktreeTool
from xana.tools.builtin.glob_tool import GlobTool
from xana.tools.builtin.grep_tool import GrepTool
from xana.tools.builtin.memory_search import MemorySearchTool
from xana.tools.builtin.memory_write import MemoryWriteTool
from xana.tools.builtin.read import ReadTool
from xana.tools.builtin.skill_load import SkillLoadTool
from xana.tools.builtin.todo_write import TodoWriteTool
from xana.tools.builtin.tool_search import ToolSearchTool
from xana.tools.builtin.web_fetch import WebFetchTool
from xana.tools.builtin.report_goal import ReportGoalTool
from xana.tools.builtin.web_search import WebSearchTool
from xana.tools.builtin.xana_search import (DeepResearchTool, WebOpenUrlTool, XanaSearchClient,
                                            XanaWebSearchTool)
from xana.tools.builtin.write import WriteTool
from xana.tools.interfaces import ToolRegistry
from xana.tools.registry import InMemoryToolRegistry


_INJECTOR_TOP_K = 3
_INJECTOR_BUDGET_CHARS = 2000
_FAKE_PROVIDER = "fake"
_logger = logging.getLogger(__name__)


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
    scheduler: FileSchedulerService | None = None
    session_resolver: SessionResolver | None = None
    main_writer: MainWriter | None = None
    run_jsonl_writer: RunJsonlWriter | None = None
    write_queue: WriteQueue | None = None
    write_consumer: WriteQueueConsumer | None = None
    write_consumer_task: asyncio.Task | None = None
    budget_tracker: BudgetTracker | None = None
    agent_spawner: Any = None
    subagent_dispatcher: Any = None
    agent_registry: Any = None
    ledger: Any = None
    episode_runner: Any = None
    meta_service: Any = None


class _CompositeStateRepository(StateRepository):
    def __init__(self, sessions: SessionStore, runs: RunStore) -> None:
        self._sessions = sessions
        self._runs = runs
    def sessions(self) -> SessionStore: return self._sessions
    def runs(self) -> RunStore: return self._runs


def _xana_home() -> Path:
    override = os.environ.get("XANA_HOME")
    return Path(override).expanduser() if override else Path.home() / ".xana"


def _search_db_path(project_root: Path) -> Path:
    """L1: 持久索引文件 ~/.xana/sessions/search-{project_hash}.db。

    文件库让索引跨重启保留(connect 时按行水合),运行期靠 Write/Edit 的
    reindex 钩子增量更新,boot 的 rebuild_from_tree 只做幂等校准。
    """
    digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:16]
    sessions = _xana_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions / f"search-{digest}.db"


async def boot(
    *,
    project_root: Path,
    config_provider: DefaultConfigProvider | None = None,
    extension_registry: ExtensionRegistry | None = None,
    history_root: Path | None = None,
) -> BootResult:
    project_root = Path(project_root).resolve()

    engine = SQLiteMemorySearchEngine(_search_db_path(project_root))
    tree = MarkdownMemoryTree(root=project_root, entrypoint="README_ai.md", search_engine=engine)
    await tree.load()
    await engine.rebuild_from_tree(tree)
    injector = SearchMemoryInjector(engine, top_k=_INJECTOR_TOP_K, budget_chars=_INJECTOR_BUDGET_CHARS)
    injector_registry = DefaultMemoryInjectorRegistry()
    await injector_registry.register(injector, priority=100)

    # §1.5 Compact-as-PH: construct ContextSDK FIRST, then back the legacy
    # InMemoryActiveContextStore with it so writes flow into both stores and
    # reads are served from the SDK (canonical). P3-D.
    from xana.context.sdk import ContextSDK
    context_sdk = ContextSDK()
    # Bug B fix: construct the harness early so we can wire its
    # _session_prompt_tokens cache to the active-context store's on_clear
    # callback (popping stale entries when /clear runs). See
    # docs/design/session-and-card-state.md §1.5.
    _harness = DefaultHarness(extension_registry=extension_registry)
    def _on_clear_drop_token_cache(sid: str, agent: str) -> None:
        _harness._session_prompt_tokens.pop((sid, agent), None)

    active_ctx_inner = InMemoryActiveContextStore(
        context_sdk=context_sdk,
        on_clear=_on_clear_drop_token_cache,
    )
    main_writer: MainWriter | None = None
    run_jsonl_writer: RunJsonlWriter | None = None
    mirror: HistoryMirror
    active_ctx: ActiveContextStore
    if history_root is not None:
        history_root = Path(history_root)
        file_mirror = FileHistoryMirror(root=history_root)
        await replay_and_detect_interrupts(file_mirror, active_ctx_inner, FileMemoryReplayer())
        main_writer = MainWriter(inner_store=active_ctx_inner, inner_mirror=file_mirror)
        await main_writer.start()
        active_ctx = main_writer.store
        mirror = main_writer.mirror
        run_jsonl_writer = RunJsonlWriter(sessions_root=history_root)
    else:
        mirror = InMemoryHistoryMirror()
        active_ctx = active_ctx_inner

    # Group C: write queue + single consumer for PH replacement + replace_range.
    # CRITICAL (spec section 11): consumer runs as a SEPARATE asyncio task
    # so sync mode does not deadlock when _invoke enqueues during a run.
    write_queue = WriteQueue()
    write_consumer = WriteQueueConsumer(
        write_queue, store=active_ctx, mirror=mirror,
    )
    write_consumer_task = asyncio.create_task(
        write_consumer.run(), name="xana-write-queue-consumer",
    )

    tools = InMemoryToolRegistry()
    await tools.register(MemorySearchTool(engine))
    await tools.register(MemoryWriteTool(tree=tree, engine=engine))
    await tools.register(ReadTool())
    # L2 门禁:裸写 L2/ 规则目录与 ~/.xana/agents Agent 文档目录被拒
    # (protected_dir 多目录,§6.3);root 内 .md 写后走 post_tool_use 增量
    # reindex(memory_tree)。
    l2_root = project_root / "L2"
    agents_root = _xana_home() / "agents"
    await tools.register(EditTool(memory_tree=tree, protected_dir=(l2_root, agents_root)))
    await tools.register(WriteTool(memory_tree=tree, protected_dir=(l2_root, agents_root)))
    await tools.register(GlobTool())
    await tools.register(GrepTool())
    await tools.register(BashTool())
    await tools.register(ToolSearchTool(tool_registry=tools))
    await tools.register(ReportGoalTool())  # 仅目标模式会话可见(harness 按 PromptProfile 过滤)
    scheduler = FileSchedulerService(
        path=_xana_home() / "scheduled_tasks.json", session_id="default")
    await tools.register(CronCreateTool(scheduler))
    await tools.register(CronListTool(scheduler))
    await tools.register(CronDeleteTool(scheduler))

    todo_root = Path.home() / ".xana" / "sessions"
    worktree_root = Path.home() / ".xana" / "worktrees"
    session_config_root = Path.home() / ".xana" / "sessions"
    session_config_store: SessionConfigStore = FileSessionConfigStore(root=session_config_root)
    todo_store = TodoStore(root=todo_root)
    await tools.register(TodoWriteTool(todo_store))
    await tools.register(EnterPlanModeTool(session_config_store=session_config_store))
    await tools.register(EnterWorktreeTool(session_config_store=session_config_store, worktree_root=worktree_root))

    skills = InMemorySkillRegistry()

    # bundled skills:独立 registry,零每轮注入;全文经 skill_load 按需取,
    # 列表段由 SkillsListingContributor 注入(仅当调用方未传 extension_registry)。
    # §5.1 文件 SKILL.md 与 bundled 注册进同一个 registry、走同一个
    # skill_load 两阶段路径(不新建平行机制);坏文档记 warning 不阻塞 boot。
    from xana.skills.markdown_loader import FileSkillLoader, scan_skill_docs
    _doc_parser = MarkdownDocParser()
    bundled_skills = InMemorySkillRegistry()
    for bundled in (LoopSkill(), ScheduleSkill(), SkillCreatorSkill()):
        await bundled_skills.register(bundled)
    _file_skills, _skill_doc_errors = await scan_skill_docs(
        _xana_home() / "skills", loader=FileSkillLoader(parser=_doc_parser),
    )
    for _err in _skill_doc_errors:
        _logger.warning("SKILL.md 加载失败: %s", _err)
    for _file_skill in _file_skills:
        await bundled_skills.register(_file_skill)
    await tools.register(SkillLoadTool(skills=bundled_skills))
    if extension_registry is None:
        extension_registry = (ExtensionRegistryBuilder()
                              .add(SkillsListingContributor(skills=bundled_skills))
                              .build())

    cp = config_provider or DefaultConfigProvider()
    runtime_config = await cp.load(project_root)
    system_cfg = cp.load_system_config()
    model_thresholds = {e.name: derive_compact_thresholds(e) for e in system_cfg.models}
    # SessionResolver — single source of truth for (adapter_id, thread_id) -> sid.
    # Seeded from system_cfg.sessions (read-once at boot per I16); mirror-writes
    # back to the same system.json path.
    _sys_json_path = default_system_json_source().path
    assert _sys_json_path is not None
    session_resolver = SessionResolver(
        system_config=system_cfg,
        system_json_path=_sys_json_path,
    )
    # 联网:配置了 xana-search 服务时用 web_search(只返回搜索摘要)/ web_open_url(读全文 + 按目标提炼)
    # / deep_research(服务端后台研究);未配置时沿用旧的 web_search / web_fetch(brave key 走
    # web_search.brave_search_key)
    if system_cfg.web_search.xana_search_url:
        search_client = XanaSearchClient(system_cfg.web_search.xana_search_url,
                                         system_cfg.web_search.xana_search_token)
        await tools.register(XanaWebSearchTool(search_client))
        await tools.register(WebOpenUrlTool(search_client))
        await tools.register(DeepResearchTool(search_client))
    else:
        await tools.register(WebSearchTool(
            session_config_store=session_config_store,
            brave_api_key=(system_cfg.web_search.brave_search_key or None)))
        await tools.register(WebFetchTool())
    provider = runtime_config.default_model.provider
    model = runtime_config.default_model.model
    client = _build_model_client(provider, model, cp)
    # §4.3: 消费 SystemConfig.limit().max_concurrent —— per-provider 并发位,
    # InMemoryModelRegistry 外包 semaphore;未配置的 provider 不限制。
    model_registry = InMemoryModelRegistry(concurrency_limits={
        name: limit.max_concurrent for name, limit in system_cfg.limits.items()
    })
    await model_registry.register(provider, client)

    # §4.3 预算护栏:concrete helper(boot 组装,不抽 ABC),注入 harness ctx.metadata。
    budget_tracker = BudgetTracker()

    state: StateRepository
    run_store: RunStore | None = None
    if history_root is not None:
        run_store = FileRunStore(root=history_root.parent / "runs")
        state = _CompositeStateRepository(sessions=InMemorySessionStore(), runs=run_store)
    else:
        state = InMemoryStateRepository()

    gateway = DefaultAgentGateway(
        agents={},
        router=DefaultAgentRequestRouter(default_agent="chat"),
    )
    # P5: wire RunStore + RunJsonlWriter so /resume + cancel_all have backing.
    gateway.run_store = run_store
    gateway.run_jsonl_writer = run_jsonl_writer
    # §4.2: spawn/lineage 首个实现(gateway 内),构造注入。
    from xana.agent_adapter.gateway import GatewayAgentSpawner, GatewaySubagentDispatcher
    agent_spawner = GatewayAgentSpawner(
        gateway=gateway,
        session_store=state.sessions(),
        context_sdk=context_sdk,
        active_context_store=active_ctx,
    )
    # §6.1/§6.2: 进化账本 + episode 状态机(boot 组装的 concrete helper,
    # 不抽 ABC)。Ledger 落 {XANA_HOME}/evolution/ledger.jsonl。
    from xana.evolution.episode import EpisodeGatedSpawner, EpisodeRunner
    from xana.evolution.ledger import Ledger
    ledger = Ledger()
    episode_runner = EpisodeRunner(ledger=ledger, run_store=run_store)
    # spawn/分叉门禁(§6.2 机制兜底):landing/closing/closed 期类型化拒绝。
    gated_spawner = EpisodeGatedSpawner(inner=agent_spawner, runner=episode_runner)
    subagent_dispatcher = GatewaySubagentDispatcher(
        spawner=gated_spawner,
        session_store=state.sessions(),
    )
    gateway.agent_spawner = gated_spawner
    gateway.subagent_dispatcher = subagent_dispatcher

    # P5.5/5.6: after replay, scan runs/ dir → PH-replace completed, mark
    # unfinished as resumable (exit_reason=shutdown_force).
    if history_root is not None and run_store is not None:
        from xana.state.startup_scan import (
            mark_unfinished_as_resumable,
            replace_completed_placeholders,
            scan_runs_dir,
        )
        runs_root = history_root / "runs"
        report = scan_runs_dir(runs_root)
        # Enumerate session_ids by session jsonl files at history_root.
        session_ids = sorted({p.stem for p in history_root.glob("*.jsonl")})
        # If no session files exist yet, still process scan with a synthetic
        # "default" session_id so unfinished runs are marked — RunRecord needs
        # a session_id field but list_unfinished does not filter by it.
        if not session_ids:
            session_ids = ["default"]
        for sid in session_ids:
            await replace_completed_placeholders(
                report=report, runs_root=runs_root,
                session_id=sid, agent_name="chat", store=active_ctx,
            )
        # mark_unfinished_as_resumable is idempotent per-run (get → update vs
        # create); call once with the first session_id to avoid duplicate
        # create errors across sessions.
        await mark_unfinished_as_resumable(
            report=report, store=run_store,
            session_id=session_ids[0], agent_name="chat",
        )

    progress_sink = AdapterProgressSink(gateway.adapters)

    chat_agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=_harness,
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
        todo_store=todo_store,
        context_window=runtime_config.default_model.context_window,
        force_compact=runtime_config.default_model.force_compact,
        model_thresholds=model_thresholds,
        main_writer=main_writer,
        run_jsonl_writer=run_jsonl_writer,
        write_queue=write_queue,
        budget_tracker=budget_tracker,
        episode_runner=episode_runner,
    )

    gateway.agents["chat"] = chat_agent

    # §5.1 Agent 文档:~/.xana/agents/*/AGENT.md → AgentSpec → ChatAgent
    # (共享 harness/stores,spec 来自文档:identity=工作流正文,tools/skills
    # 白名单映射进现有字段)。builtin.chat 保持默认不变;坏文档记 warning。
    from xana.agents.contracts import AgentSpec
    from xana.agents.registry import FileAgentRegistry

    def _doc_agent_factory(spec: AgentSpec) -> Agent:
        return ChatAgent(
            spec=spec,
            harness=_harness,
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
            main_writer=main_writer,
            run_jsonl_writer=run_jsonl_writer,
            write_queue=write_queue,
            todo_store=todo_store,
            budget_tracker=budget_tracker,
            episode_runner=episode_runner,
        )

    agent_registry = FileAgentRegistry(
        root=_xana_home() / "agents",
        parser=_doc_parser,
        agent_factory=_doc_agent_factory,
    )
    for _err in await agent_registry.load():
        _logger.warning("AGENT.md 加载失败: %s", _err)
    for _spec in await agent_registry.list_specs():
        _doc_agent = await agent_registry.get(_spec.component.name)
        if _doc_agent is not None:
            gateway.agents[_spec.component.name] = _doc_agent

    # §6.3: 元 agent 服务。迭代对象 = 首个文档 agent(没有则 builtin.chat);
    # cron 幂等安装,首个 episode 时创建。
    from xana.evolution.meta import MetaEvolutionService
    _meta_agent_name = "chat"
    _agent_specs = await agent_registry.list_specs()
    if _agent_specs:
        _meta_agent_name = _agent_specs[0].component.name
    meta_service = MetaEvolutionService(
        scheduler=scheduler,
        gateway=gateway,
        episode_runner=episode_runner,
        ledger=ledger,
        todo_store=todo_store,
        agents_root=_xana_home() / "agents",
        agent_name=_meta_agent_name,
    )

    # §1.5 Compact-as-PH: ContextSDK constructed above and bound into the
    # legacy active_context_store; same instance is wired into the
    # command router so /compact reaches the same buffer.
    command_router = DefaultCommandRouter(
        session_config_store=session_config_store,
        active_context_store=active_ctx,
        gateway=gateway,
        context_sdk=context_sdk,
        # 只列同 provider 的模型:进程内只注册了当前 provider 的 client,
        # 跨 provider 的模型名发给它会出错。
        available_models=tuple(e.name for e in system_cfg.models if e.type == provider) or (model,),
        available_search_providers=("native", "brave", "duckduckgo"),
        available_efforts=KIMI_EFFORTS if provider.startswith("kimi") else DEEPSEEK_EFFORTS,
        model_registry=model_registry,
        provider=provider,
        model=model,
        progress_sink=progress_sink,
        write_queue=write_queue,
    )
    await gateway.register_command_router(command_router)

    return BootResult(
        gateway=gateway,
        agents=dict(gateway.agents),
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
        scheduler=scheduler,
        session_resolver=session_resolver,
        main_writer=main_writer,
        run_jsonl_writer=run_jsonl_writer,
        write_queue=write_queue,
        write_consumer=write_consumer,
        write_consumer_task=write_consumer_task,
        budget_tracker=budget_tracker,
        agent_spawner=gated_spawner,
        subagent_dispatcher=subagent_dispatcher,
        agent_registry=agent_registry,
        ledger=ledger,
        episode_runner=episode_runner,
        meta_service=meta_service,
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
    if "deepseek" in provider:
        # type 匹配用包含而非前缀:wizard 提示的名字是 "anthropic_deepseek",
        # 用户手填的多是 "deepseek-v4-flash" — 两种都要命中。
        system = cp.load_system_config()
        entry = system.model(model)
        if entry is None or not entry.api_key:
            _logger.warning(
                "deepseek 模型 %s 的 entry 缺失或 api_key 为空 — 当前回落 fake client;"
                "请检查 system.json models[]",
                model,
            )
            return _fake()
        config = (
            AnthropicDeepSeekConfig(api_key=entry.api_key, base_url=entry.base_url)
            if entry.base_url
            else AnthropicDeepSeekConfig(api_key=entry.api_key)
        )
        return AnthropicDeepSeekClient(config)
    if provider.startswith("kimi"):
        # kimi 走 OAuth(见 `xana login`),不用 api_key;token 到期自动刷新。
        # 未登录时回落 fake 并给出明确指引,与 deepseek 缺 key 的行为一致。
        system = cp.load_system_config()
        entry = system.model(model)
        store = KimiTokenStore()
        tokens = store.load()
        if tokens is None:
            _logger.warning(
                "kimi 模型 %s 已配置但未登录 — 请先运行 `xana login`;当前回落 fake client",
                model,
            )
            return _fake()
        base_url = (entry.base_url if entry and entry.base_url else None) or kimi_api_base(
            tokens.region
        )
        return KimiClient(KimiConfig(base_url=base_url), token_store=store)
    return _fake()


def _fake() -> FakeModelClient:
    return FakeModelClient(ScriptedTurn(usage_input_tokens=4, usage_output_tokens=8))
