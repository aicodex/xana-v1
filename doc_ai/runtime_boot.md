---
title: runtime boot
type: doc
status: active
---

# runtime boot

## 上级模块

- [runtime](runtime.md) — 模块设计

## 代码锚点

- [boot.py](../src/xana/runtime/boot.py) — `boot()` 唯一组合点（Rule 4：全项目只有这里调 `register()`）
- [paths.py](../src/xana/config/paths.py) — `xana_home()` / `project_id()` / `project_home()`，路径唯一解析点

## 职责

`boot()` 把第一条核心流程的全部依赖在一处组装好，返回 `BootResult` 供 CLI 和测试自省。它是进程内唯一的组合点，启动序列就是 `boot()` 函数体本身，不另设启动 ABC。

## 组装顺序（boot.py）

1. **memory 底座**：`MarkdownMemoryTree(project_root, entrypoint="README_ai.md").load()` → `SQLiteMemorySearchEngine(projects/<id>/sessions/search-{project_hash}.db)` 建索引（`rebuild_from_tree` 幂等校准） → `SearchMemoryInjector`（top_k=3, budget 2000 字符）注册进 `DefaultMemoryInjectorRegistry`。
2. **ContextSDK + active context**：先构造 `ContextSDK`，再构造 `DefaultHarness`（提前构造是为了把 harness 的 `_session_prompt_tokens` 缓存挂到 active context 的 `on_clear` 回调，`/clear` 时清掉过期条目）。`InMemoryActiveContextStore` 以 ContextSDK 为 canonical 后端。
3. **history 分叉**（`history_root` 非空才落盘）：
   - 有 `history_root`：`FileHistoryMirror` + `replay_and_detect_interrupts` 重放恢复 → `MainWriter`（inner store + inner mirror，后台同步写）→ 另建 `RunJsonlWriter(sessions_root=history_root)`。
   - 无 `history_root`：`InMemoryHistoryMirror`，writer 全为 `None`（内存态，无持久化）。
4. **WriteQueue**：`WriteQueue` + `WriteQueueConsumer` 作为**独立 asyncio task**（`xana-write-queue-consumer`）启动——spec §11 要求 consumer 与同步模式的 `_invoke` 解耦，否则 replace_range 入队时死锁。
5. **Tool registry**：`InMemoryToolRegistry` 注册全部内置工具。关键注入：
   - `FileSchedulerService(path=projects/<id>/scheduled_tasks.json, session_id="default")` → `CronCreateTool/CronListTool/CronDeleteTool`；
   - `FileSessionConfigStore(root=projects/<id>/sessions)`（todo 存储同目录，worktree 根为 `projects/<id>/worktrees`） → `EnterPlanModeTool` / `EnterWorktreeTool`；同一个 store 实例后面也进 `BootResult` 和 `DefaultCommandRouter`；
   - `WebSearchTool` 注入 `brave_search_key`（来自 system.json）；
   - 仅当 `project_id() == "default"`：注册 `ProjectManageTool`（工具组 `projects`，default 项目是管理台）；
   - Write / Edit 的 `protected_dir` = (`project_root/L2`, `{XANA_HOME}/agents`, `project_root/.xana/agents`)，两级 agent 目录只能经 `agent_define` 写。
5b. **Skills 文档加载**：构造共享 `MarkdownDocParser`；`bundled_skills = InMemorySkillRegistry()` 注册 bundled skill（loop / schedule / skill_creator / agent_creator，default 项目另加 project_creator）；`scan_skill_docs({XANA_HOME}/skills)` 加载文件 SKILL.md（与 bundled **同一个 registry、同一个 `skill_load` 两阶段路径**），坏文档记 warning 跳过。`SkillLoadTool(skills=bundled_skills)` 注册进 tool registry；调用方未传 `extension_registry` 时自动挂 `SkillsListingContributor(skills=bundled_skills)`。harness 的 `skill_registry` 仍为空——文件 skill 不做每轮全文注入，见 [skills_runtime](skills_runtime.md)。
6. **config 加载**：`DefaultConfigProvider().load(project_root)` 产出 `RuntimeConfig`（用户级 system.json + 本项目 config.json，见 [config_merge](config_merge.md)）；`load_system_config()` 拿 pydantic `SystemConfig` 缓存，其 `agent_defaults` 传给 `FileAgentRegistry` 与 `build_chat_agent_spec()`。空 models[] 或未配 key 时回落 `provider="fake"`（见 `_build_model_client`：deepseek 按 api_key 构造 `AnthropicDeepSeekClient`，kimi 走 OAuth token 未登录回落 fake，其余一律 `FakeModelClient`）。
7. **SessionResolver**：以 `system_cfg` 播种、项目 config.json（`cp.project_config_path`）为镜像路径、`write_project_sessions` 为 writer 构造——(adapter_id, thread_id)→sid 的单一权威，boot 时一次性读配置（I16），映射只写回项目 config.json 的 `sessions`。
8. **StateRepository 分叉**（boot.py:128 `_CompositeStateRepository`）：
   - 有 `history_root`：`FileRunStore(root=history_root.parent/"runs")` + `InMemorySessionStore` 包进 `_CompositeStateRepository`；
   - 无：整体 `InMemoryStateRepository`。
9. **startup_scan**（有 `history_root` 才执行）：`scan_runs_dir(history_root/"runs")` → 对每个 session jsonl 名的 sid 调 `replace_completed_placeholders`（completed run 的 PH 替换为真实消息）→ `mark_unfinished_as_resumable`（未完成的标 `paused` + `exit_reason=shutdown_force`，幂等可重入）。详见 [state_resume](state_resume.md)。
10. **Gateway + ChatAgent**：`DefaultAgentGateway`（router 默认 agent "chat"），`gateway.run_store` / `gateway.run_jsonl_writer` 注入以便 `/resume` 与 `cancel_all` 有落盘支撑；`ChatAgent` 拿到全部 registry、state、writer 依赖。
10b. **Agent 文档加载**：`FileAgentRegistry(roots=(("user", {XANA_HOME}/agents), ("project", project_root/.xana/agents)), parser=共享 MarkdownDocParser, agent_factory=共享 harness/stores 的 ChatAgent 工厂, table=agents, reserved=内置 agent 名, defaults=system_cfg.agent_defaults)`——registry 直接维护 gateway 共用的 `agents` 表（热加载，不是一次性拷贝），首次 `load()` 扫描两级 `*/AGENT.md`，坏文档记 warning 跳过。内置 chat 的 spec 由 `build_chat_agent_spec(system_cfg.agent_defaults)` 构造。registry 实例进 `BootResult.agent_registry`，同时注入 gateway、`DefaultCommandRouter`、`agent_run`、`agent_define`（`roots={user, project}`）。详见 [agents_registry](agents_registry.md)。
11. **Command router**：`DefaultCommandRouter` 注入 `context_sdk`（与 active context 同一个实例，`/compact` 命中同一缓冲）、`session_config_store`、可用模型/搜索源列表等。

## 出口：BootResult

`BootResult`（boot.py:103）是一个 dataclass，字段即上述全部组装产物：`gateway`、`agents`、`memory_tree`、`search_engine`、`active_context_store`、`history_mirror`、`memory_injector_registry`、`tool_registry`、`skill_registry`、`model_registry`、`state_repository`、`session_config_store`、`progress_sink`、`provider`、`model`，以及可空尾部 `scheduler` / `session_resolver` / `main_writer` / `run_jsonl_writer` / `write_queue` / `write_consumer` / `write_consumer_task` / `budget_tracker` / `agent_spawner` / `subagent_dispatcher` / `agent_registry`。CLI（serve/chat）与测试从这里取件，不做二次组装。

## 不变量

- `boot()` 是唯一组合点；`extension_registry=None` 时行为与未引入 Extension API 前一致（内部自动补 `SkillsListingContributor`）。
- 所有 `~/.xana` 下路径统一走 `config/paths`：用户级（skills、agents、credentials）经 `xana_home()`，项目私有（sessions、runs、scheduled_tasks.json、worktrees、evolution）经 `project_home()`；skills 与 agents 文档的坏文件都记 warning 跳过，不阻塞 boot。
- `history_root` 是唯一持久化开关：不给就全是内存实现，不静默写盘。
- 模型选择权威在 `DefaultConfigProvider` + `_build_model_client`；ChatAgent 的 provider/model 钉死为实际注册值，保证 `config_snapshot["provider"]` 在 harness 内可解析。

## 验收要求

- 输入/输出与 [runtime](./runtime.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
