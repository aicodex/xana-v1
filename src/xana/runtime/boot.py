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
from dataclasses import dataclass
from pathlib import Path

from xana.config.paths import DEFAULT_PROJECT, project_home, project_id, xana_home
from xana.config.project_config import write_project_sessions
from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.progress import AdapterProgressSink
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.researcher import RESEARCHER_AGENT
from xana.agents.interfaces import Agent
from xana.config.loader import (
    DefaultConfigProvider,
    derive_compact_thresholds,
)
from xana.extension_api.builtin.agent_listing import AgentListingContributor
from xana.extension_api.builtin.skills_listing import SkillsListingContributor
from xana.extension_api.capabilities import AgentRunner
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
)
from xana.skills.bundled.coding import CodingSkill
from xana.skills.bundled.loop import LoopSkill
from xana.skills.bundled.schedule import ScheduleSkill
from xana.skills.bundled.agent_creator import AgentCreatorSkill
from xana.skills.bundled.project_creator import ProjectCreatorSkill
from xana.skills.bundled.skill_creator import SkillCreatorSkill
from xana.skills.bundled.tool_creator import ToolCreatorSkill
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
from xana.memory.recall import ProjectMemoryInjector
from xana.memory.store import ProjectMemoryStore
from xana.runtime.memory_extract import ModelMemoryExtractor
from xana.runtime.memory_selector import ModelMemorySelector
from xana.runtime.scheduler import FileSchedulerService, ScheduledTaskDispatcher
from xana.runtime.session_resolver import SessionResolver
from xana.agent_adapter.agent_runner import GatewayAgentRunner
from xana.tools.builtin.agent_define import AgentDefineTool
from xana.tools.builtin.agent_run import AgentRunTool
from xana.evolution.evals import EvalRunner
from xana.tools.builtin.eval_tools import EvalRunTool, EvalScoreTool
from xana.tools.builtin.tool_define import ToolDefineTool
from xana.tools.builtin.trace_view import TraceViewTool
from xana.tools.builtin.bash import BashTool
from xana.tools.builtin.project_manage import ProjectManageTool
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
from xana.tools.builtin.notify_user import NotifyUserTool
from xana.tools.builtin.report_goal import ReportGoalTool
from xana.tools.builtin.web_search import WebSearchTool
from xana.tools.builtin.xana_search import (BackgroundResearchTool, WebOpenUrlTool, XanaSearchClient,
                                            XanaWebSearchTool)
from xana.tools.builtin.write import WriteTool
from xana.tools.interfaces import Tool, ToolRegistry
from xana.tools.environment import TapeToolEnvironment
from xana.tools.file_registry import FileToolRegistry
from xana.tools.toolsets import BUILTIN_TOOLSETS, unknown_names


_FAKE_PROVIDER = "fake"
BUILTIN_AGENT_NAMES = ("chat", RESEARCHER_AGENT)   # 内置 agent:不能被 AGENT.md 覆盖或删除
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
    agent_runner: AgentRunner | None = None
    scheduler_dispatch: ScheduledTaskDispatcher | None = None
    main_writer: MainWriter | None = None
    run_jsonl_writer: RunJsonlWriter | None = None
    write_queue: WriteQueue | None = None
    write_consumer: WriteQueueConsumer | None = None
    write_consumer_task: asyncio.Task | None = None
    budget_tracker: BudgetTracker | None = None
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


def _search_db_path(project_root: Path) -> Path:
    """记忆树检索索引(memory_search)的持久文件 projects/<id>/sessions/search-{project_hash}.db。

    文件库让索引跨重启保留(connect 时按行水合),运行期靠 Write/Edit 的
    reindex 钩子增量更新,boot 的 rebuild_from_tree 只做幂等校准。
    """
    digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:16]
    sessions = project_home() / "sessions"
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
    # 项目记忆(docs/design/memory-recall-design.md):memory/ 目录;注入器在模型注册后接入(召回要调模型挑选)
    memory_store = ProjectMemoryStore(project_root)
    injector_registry = DefaultMemoryInjectorRegistry()

    # §1.5 Compact-as-PH: the ContextSDK buffer is the single authority for session context;
    # InMemoryActiveContextStore (below) reads and writes it.
    from xana.context.sdk import ContextSDK
    context_sdk = ContextSDK()

    # bundled skills:独立 registry,零每轮注入;全文经 skill_load 按需取,
    # 列表段由 SkillsListingContributor 注入(仅当调用方未传 extension_registry)。
    # §5.1 文件 SKILL.md 与 bundled 注册进同一个 registry、走同一个
    # skill_load 两阶段路径(不新建平行机制);坏文档记 warning 不阻塞 boot。
    from xana.skills.markdown_loader import FileSkillLoader, scan_skill_docs
    _doc_parser = MarkdownDocParser()
    bundled_skills = InMemorySkillRegistry()
    for bundled in (LoopSkill(), ScheduleSkill(), SkillCreatorSkill(), AgentCreatorSkill(), ToolCreatorSkill(),
                    CodingSkill()):
        await bundled_skills.register(bundled)
    if project_id() == DEFAULT_PROJECT:   # default 项目是管理台:在聊天里创建 / 管理项目(project-design §7)
        await bundled_skills.register(ProjectCreatorSkill())
    _file_skills, _skill_doc_errors = await scan_skill_docs(
        xana_home() / "skills", loader=FileSkillLoader(parser=_doc_parser),
    )
    for _err in _skill_doc_errors:
        _logger.warning("SKILL.md 加载失败: %s", _err)
    for _file_skill in _file_skills:
        await bundled_skills.register(_file_skill)
    # agent 表与工具目录先建空容器(后面逐个注册):默认扩展(可委派 agent 列表)持有的是活的引用。
    cp = config_provider or DefaultConfigProvider()
    runtime_config = await cp.load(project_root)
    system_cfg = cp.load_system_config()
    agents: dict[str, Agent] = {}
    # 工具目录 = 内置 / 扩展登记的工具 + 两级目录 TOOL.md 派生工具(热加载,iteration-agent-design §5.3)
    tool_dirs = (xana_home() / "tools", project_root / ".xana" / "tools")
    evals_dir = project_root / ".xana" / "evals"    # 评测套件:样本、真值、打分命令对 agent 只读(§9.3)
    tools = FileToolRegistry(roots=(("user", tool_dirs[0]), ("project", tool_dirs[1])), project_root=project_root)
    # 文件 agent:两级目录(用户级 < 项目级),热加载,直接维护共用的 agents 表(agent-delegation-design §7)。
    # 工厂在下文定义、首次 load 时才调用。
    from xana.agents.registry import FileAgentRegistry
    agent_registry = FileAgentRegistry(
        roots=(("user", xana_home() / "agents"), ("project", project_root / ".xana" / "agents")),
        parser=_doc_parser,
        agent_factory=lambda spec: _doc_agent_factory(spec),
        table=agents,
        reserved=BUILTIN_AGENT_NAMES,
        defaults=system_cfg.agent_defaults,     # 项目给 agent 的默认配置(project-design §8)
    )
    if extension_registry is None:
        extension_registry = (ExtensionRegistryBuilder()
                              .add(SkillsListingContributor(skills=bundled_skills))
                              .add(AgentListingContributor(agents=agents, agent_registry=agent_registry,
                                                           tool_registry=tools))
                              .build())

    provider = runtime_config.default_model.provider
    model = runtime_config.default_model.model
    client = _build_model_client(provider, model, cp)
    # §4.3: 消费 SystemConfig.limit().max_concurrent —— per-provider 并发位,
    # InMemoryModelRegistry 外包 semaphore;未配置的 provider 不限制。
    model_registry = InMemoryModelRegistry(concurrency_limits={
        name: limit.max_concurrent for name, limit in system_cfg.limits.items()
    })
    await model_registry.register(provider, client)

    # 记忆召回:快速模型(model_tiers.fast 且属于当前 provider)挑选,否则用当前模型;超时退回字面检索
    _fast = system_cfg.model(system_cfg.model_tiers.fast) if system_cfg.model_tiers.fast else None
    _memory_model = _fast.name if _fast is not None and _fast.type == provider else model
    memory_selector = ModelMemorySelector(model_registry=model_registry, provider=provider, model=_memory_model)
    await injector_registry.register(ProjectMemoryInjector(
        memory_store, selector=memory_selector, mode=system_cfg.memory.recall,
        timeout_s=system_cfg.memory.select_timeout_s), priority=100)
    # 记忆的后台提取(docs/design/memory-dream-design.md §3.1):每轮结束后把漏记的写进记忆;0 = 关闭
    memory_extractor = (ModelMemoryExtractor(
        model_registry=model_registry, provider=provider, model=_memory_model,
        store=memory_store, every=system_cfg.memory.extract_every) if system_cfg.memory.extract_every > 0 else None)

    # 扩展注册表须在构造 harness 之前组装完毕(harness 构造时即持有它)。
    # harness 提前构造:/clear 时经 on_clear 丢弃它记下的 prompt 占用
    # (docs/design/session-and-card-state.md §1.5)。
    # 评测 run 的工具环境:录制回放读 sessions/runs(iteration-agent-design §6)
    _harness = DefaultHarness(
        extension_registry=extension_registry, memory_extractor=memory_extractor,
        tool_environment=TapeToolEnvironment(runs_dir=Path(history_root) / "runs") if history_root is not None else None)

    active_ctx_inner = InMemoryActiveContextStore(
        context_sdk=context_sdk,
        on_clear=_harness.forget_prompt_tokens,
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

    # Group C: write queue + single consumer for PH replacement.
    # CRITICAL (spec section 11): consumer runs as a SEPARATE asyncio task
    # so sync mode does not deadlock when _invoke enqueues during a run.
    write_queue = WriteQueue()
    write_consumer = WriteQueueConsumer(
        write_queue, store=active_ctx, mirror=mirror,
    )
    write_consumer_task = asyncio.create_task(
        write_consumer.run(), name="xana-write-queue-consumer",
    )


    # 工具目录(docs/design/agent-composition-design.md §3.1–3.2):内置工具组先登记;
    # system.json tools.disabled 里的组 / 工具不注册,对所有 agent 都不存在。
    for _toolset in BUILTIN_TOOLSETS:
        await tools.register_toolset(_toolset)
    _disabled_tools = set(system_cfg.tools.disabled)

    async def _register(tool: Tool) -> None:
        sp = tool.spec()
        if sp.component.name in _disabled_tools or (sp.toolset and sp.toolset in _disabled_tools):
            return
        await tools.register(tool)
    await _register(MemorySearchTool(engine))
    await _register(MemoryWriteTool(store=memory_store, tree=tree))
    await _register(ReadTool())
    # 写保护:记忆目录 memory/ 只能经 memory_write 写,两级 agent 文档目录只能经 agent_define 写;
    # root 内 .md 写后增量 reindex(memory_tree)。
    agent_dirs = (xana_home() / "agents", project_root / ".xana" / "agents")
    await _register(EditTool(memory_tree=tree, protected_dir=(memory_store.dir, *agent_dirs, *tool_dirs, evals_dir)))
    await _register(WriteTool(memory_tree=tree, protected_dir=(memory_store.dir, *agent_dirs, *tool_dirs, evals_dir)))
    await _register(GlobTool())
    await _register(GrepTool())
    await _register(BashTool())
    await _register(ToolSearchTool(tool_registry=tools))
    await _register(ReportGoalTool())  # 仅目标模式会话可见(harness 按 PromptProfile 过滤)
    # SessionResolver — single source of truth for (adapter_id, thread_id) -> sid.
    # Seeded from system_cfg.sessions (read-once at boot per I16); mirror-writes
    # the map back to this project's config.json (project-design §4).
    session_resolver = SessionResolver(
        system_config=system_cfg,
        system_json_path=cp.project_config_path,
        writer=write_project_sessions,
    )
    # 定时任务记下创建时会话所在的聊天,触发时送到该聊天当前的会话(docs/design/scheduled-task-design.md)
    scheduler = FileSchedulerService(
        path=project_home() / "scheduled_tasks.json", session_id="default", route_of=session_resolver.route_of)
    await _register(CronCreateTool(scheduler, agents))
    await _register(CronListTool(scheduler))
    await _register(CronDeleteTool(scheduler))

    # 会话级数据都属于当前项目(docs/design/project-design.md §3)
    todo_root = project_home() / "sessions"
    worktree_root = project_home() / "worktrees"
    session_config_root = project_home() / "sessions"
    session_config_store: SessionConfigStore = FileSessionConfigStore(root=session_config_root)
    todo_store = TodoStore(root=todo_root)
    await _register(TodoWriteTool(todo_store))
    await _register(EnterPlanModeTool(session_config_store=session_config_store))
    await _register(EnterWorktreeTool(session_config_store=session_config_store, worktree_root=worktree_root))
    await _register(SkillLoadTool(skills=bundled_skills))
    if project_id() == DEFAULT_PROJECT:
        await _register(ProjectManageTool(tool_registry=tools, skills=bundled_skills))

    model_thresholds = {e.name: derive_compact_thresholds(e) for e in system_cfg.models}
    # 联网:配置了 xana-search 服务时用 web_search(只返回搜索摘要)/ web_open_url(读全文 + 按目标提炼)
    # / background_research(服务端后台研究,本地模型);deep_research(主模型 + 子研究员)在 agent 注册后接入。
    # 未配置时沿用旧的 web_search / web_fetch(brave key 走
    # web_search.brave_search_key)
    if system_cfg.web_search.xana_search_url:
        search_client = XanaSearchClient(system_cfg.web_search.xana_search_url,
                                         system_cfg.web_search.xana_search_token)
        await _register(XanaWebSearchTool(search_client))
        await _register(WebOpenUrlTool(search_client))
        await _register(BackgroundResearchTool(search_client))
    else:
        await _register(WebSearchTool(
            session_config_store=session_config_store,
            brave_api_key=(system_cfg.web_search.brave_search_key or None),
            native_available=provider.startswith("deepseek")))
        await _register(WebFetchTool())
    # §4.3 预算护栏:concrete helper(boot 组装,不抽 ABC),注入 harness ctx.metadata。
    budget_tracker = BudgetTracker()

    state: StateRepository
    run_store: RunStore | None = None
    if history_root is not None:
        run_store = FileRunStore(root=history_root.parent / "runs")
        state = _CompositeStateRepository(sessions=InMemorySessionStore(), runs=run_store)
        await _register(TraceViewTool(run_store=run_store, runs_dir=history_root / "runs"))
    else:
        state = InMemoryStateRepository()

    gateway = DefaultAgentGateway(
        agents=agents,
        router=DefaultAgentRequestRouter(default_agent="chat", session_config_store=session_config_store),
        agent_registry=agent_registry,
        route_of=session_resolver.route_of,
    )
    # P5: wire RunStore + RunJsonlWriter so /resume + cancel_all have backing.
    gateway.run_store = run_store
    gateway.run_jsonl_writer = run_jsonl_writer
    # §6.1/§6.2: 进化账本 + episode 状态机(boot 组装的 concrete helper,
    # 不抽 ABC)。Ledger 落当前项目的 projects/<id>/evolution/ledger.jsonl。
    from xana.evolution.episode import EpisodeRunner
    from xana.evolution.ledger import Ledger
    ledger = Ledger()
    episode_runner = EpisodeRunner(ledger=ledger, run_store=run_store)

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
        spec=build_chat_agent_spec(system_cfg.agent_defaults),
        harness=_harness,
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
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
    # (共享 harness/stores,spec 来自文档:正文=系统提示词模板,tools 为工具选择表达式,
    # skills 映射进现有字段)。builtin.chat 保持默认不变;坏文档记 warning。
    from xana.agents.contracts import AgentSpec

    def _doc_agent_factory(spec: AgentSpec, *, progress: bool = True) -> Agent:
        return ChatAgent(
            spec=spec,
            harness=_harness,
            active_context_store=active_ctx,
            history_mirror=mirror,
            memory_injector_registry=injector_registry,
            tool_registry=tools,
            model_registry=model_registry,
            state_repository=state,
            cwd=str(project_root),
            model=model,
            provider=provider,
            session_config_store=session_config_store,
            progress_sink=progress_sink if progress else None,
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

    await agent_registry.load()   # 坏文档记 warning 不阻塞 boot;之后按需热加载

    # 子 agent 运行器:agent_run 委派与 deep_research 共用(同步跑完子 run 取回最终文本,不触碰渠道)。
    # episode landing/closing/closed 期拒绝派生子 run(§6.2 机制兜底)。
    agent_runner = GatewayAgentRunner(agents=gateway.agents, session_store=state.sessions(),
                                      gate=episode_runner.ensure_gate)

    # agent 委派与创建(docs/design/agent-delegation-design.md):agent_run 同步委派;agent_define 写 / 删 AGENT.md
    # 后刷新注册表,立即生效。
    await _register(AgentRunTool(runner=agent_runner, agents=gateway.agents, agent_registry=agent_registry,
                                 tool_registry=tools))
    await _register(ToolDefineTool(registry=tools, project_root=project_root))
    # 评测套件(iteration-agent-design §8):eval_run 经 agent_runner 跑样本,eval_score 调套件的打分命令
    _evals = EvalRunner(runner=agent_runner, run_store=run_store, evals_root=evals_dir)
    await _register(EvalRunTool(_evals))
    await _register(EvalScoreTool(_evals))
    await _register(AgentDefineTool(
        roots={"user": xana_home() / "agents", "project": project_root / ".xana" / "agents"},
        agents=gateway.agents, agent_registry=agent_registry, tool_registry=tools))

    # deep_research(docs/design/lead-research-design.md):主模型当研究负责人,并行派出内置子研究员
    # (researcher:只有 web 组与 Read)。子研究员不接进度汇报(不触碰渠道)。
    if system_cfg.web_search.xana_search_url:
        from xana.agents.researcher import build_researcher_agent_spec
        from xana.tools.builtin.lead_research import LeadResearchTool
        gateway.agents[RESEARCHER_AGENT] = _doc_agent_factory(build_researcher_agent_spec(), progress=False)
        await _register(LeadResearchTool(
            runner=agent_runner,
            model_registry=model_registry, provider=provider, model=model,
            # 原始发现是研究产物不是记忆:放项目私有目录,不进记忆树(不被召回)
            findings_root=project_home() / "research", researcher_agent=RESEARCHER_AGENT))

    # 写错的名字不会生效(按名精确匹配,常见为大小写笔误),记 warning 提示修正;
    # 被系统级禁用的工具同样报出,提示该 agent 实际拿不到它。
    _all_specs = await tools.list_specs()
    _all_toolsets = await tools.list_toolsets()
    for _sp in _all_specs:
        if _sp.toolset and _sp.toolset not in {t.name for t in _all_toolsets}:
            _logger.warning("工具 %s 所属的组 %s 未登记,只能按工具名选中", _sp.component.name, _sp.toolset)
    _unknown_disabled = sorted(_disabled_tools - {t.name for t in BUILTIN_TOOLSETS}
                               - {sp.component.name for sp in _all_specs})
    if _unknown_disabled:
        _logger.info("tools.disabled 中未匹配到已注册组 / 工具的条目(可能本就未注册):%s", _unknown_disabled)
    _checked_specs = [build_chat_agent_spec(system_cfg.agent_defaults), *await agent_registry.list_specs()]
    if system_cfg.web_search.xana_search_url:
        _checked_specs.append(build_researcher_agent_spec())
    _models = {e.name for e in system_cfg.models if e.type == provider} | {model}
    for _spec in _checked_specs:
        _unknown = unknown_names((*_spec.tools, *_spec.disallowed_tools), _all_specs, _all_toolsets)
        if _unknown:
            _logger.warning("agent %s 的 tools / disallowed_tools 含未知的组或工具 %s",
                            _spec.component.name, _unknown)
        if _spec.model and _spec.model not in _models:
            _logger.warning("agent %s 指定的模型 %s 不在当前 provider(%s)已配置的模型里,调用会失败",
                            _spec.component.name, _spec.model, provider)

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
        agents_root=xana_home() / "agents",
        agent_name=_meta_agent_name,
    )

    # 定时任务到点执行(docs/design/scheduled-task-design.md):无人值守的独立子会话,按通知方式插回当前会话;
    # notify_user 经它送达通知、登记待决策的问题。serve 只负责启动 SchedulerRunner。
    scheduler_dispatch = ScheduledTaskDispatcher(
        gateway=gateway, runner=agent_runner, runs_dir=project_home() / "sessions" / "runs",
        scheduler=scheduler, session_config_store=session_config_store,
        resolver=session_resolver, meta_service=meta_service)
    await _register(NotifyUserTool(dispatcher=scheduler_dispatch))

    # §1.5 Compact-as-PH: /compact reaches the SDK through active_ctx.sdk() (same buffer).
    command_router = DefaultCommandRouter(
        session_config_store=session_config_store,
        agent_registry=agent_registry,
        active_context_store=active_ctx,
        gateway=gateway,
        # 只列同 provider 的模型:进程内只注册了当前 provider 的 client,
        # 跨 provider 的模型名发给它会出错。
        available_models=tuple(e.name for e in system_cfg.models if e.type == provider) or (model,),
        # 接了 xana-search 时搜索由它负责,没有可切换的引擎;否则第一个是缺省(只有 deepseek 系有模型内置搜索)
        available_search_providers=(() if system_cfg.web_search.xana_search_url else
                                    ("native", "brave", "duckduckgo") if provider.startswith("deepseek")
                                    else ("duckduckgo", "brave")),
        available_efforts=KIMI_EFFORTS if provider.startswith("kimi") else DEEPSEEK_EFFORTS,
        model_registry=model_registry,
        provider=provider,
        model=model,
        progress_sink=progress_sink,
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
        skill_registry=bundled_skills,
        model_registry=model_registry,
        state_repository=state,
        session_config_store=session_config_store,
        progress_sink=progress_sink,
        provider=provider,
        model=model,
        scheduler=scheduler,
        session_resolver=session_resolver,
        agent_runner=agent_runner,
        scheduler_dispatch=scheduler_dispatch,
        main_writer=main_writer,
        run_jsonl_writer=run_jsonl_writer,
        write_queue=write_queue,
        write_consumer=write_consumer,
        write_consumer_task=write_consumer_task,
        budget_tracker=budget_tracker,
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
