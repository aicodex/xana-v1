---
title: doc_ai
type: doc
status: active
---

# doc_ai

`doc_ai` contains target-state architecture. It is the stable source of truth for what the system is supposed to be.

## 系统入口

- [system](system.md) — 全局数据流、状态权威、恢复流程
- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 实现替换规则
- [io_contract_template](io_contract_template.md) — 新合约模板
- 项目（project）— 一个项目 = 一套记忆 + 自己作用域的 agents / 工具默认配置 + 一个 serve 进程；数据布局、配置分层、命令与迁移见 [project-design](../docs/design/project-design.md)。路径唯一解析点 `src/xana/config/paths.py`，项目配置分层 `src/xana/config/project_config.py`，注册 / 查询 / 启停 `src/xana/runtime/projects.py`。

## 核心模块

- [config](config.md) — 配置加载、用户级 system.json + 项目级 config.json 分层；路径解析（`config/paths.py`）与项目配置（`config/project_config.py`）。
- [runtime](runtime.md) — 进程生命周期、启动顺序、调度外壳；项目管理（`runtime/projects.py`）。
- [harness_sdk](harness_sdk.md) — turn loop、暂停/恢复/取消、事件流和 usage 快照。
- [harness_background_tasks](harness_background_tasks.md) — 独立后台任务、事件等待、持久结果交付与完成唤醒。
- [agents](agents.md) — agent spec、outer API、会话侧行为包装。
- [tools](tools.md) — 工具校验、权限、沙箱、执行边界和输出 schema。
- [skills](skills.md) — 技能元数据、激活规则、上下文/提示注入。
- [agent_adapter](agent_adapter.md) — CLI/TG/Feishu/HTTP 等外部协议翻译。
- [cli](cli.md) — 命令分发：`xana` 管理命令（setup/sync/serve/pair/project/login/logout）与 `xanac` 交互客户端。
- [llm_driver](llm_driver.md) — 模型请求、流式响应、tool call 解析、usage 提取。
- [memory](memory.md) — active context 权威、history mirror、treemem/search/injection。
- [SAG2 memory 接入](memory_sag2.md) — 算法、项目索引、评分契约和 JEV 协议已实现；[设计与验证](../docs/design/memory-sag2-design.md)记录迁移边界。status: implementing，评分指令来源待确认，尚未替换运行时。
- [context](context.md) — active context 的运行时载体：`ContextSDK` facade、有序消息与前缀压缩。

## 支撑模块

- [component](component.md) — 共享组件元数据合约（`ComponentSpec` / `ComponentSource`）。
- [policy](policy.md) — 会话权限、审批服务与沙箱合约。
- [state](state.md) — session、run、checkpoint、resume、usage 状态合约。
- extension_api — extension 合约（`src/xana/extension_api/`），尚无模块文档。
  - 8 种 Contributor ABC（`interfaces.py`）：`ContextContributor`（system prompt 片段）、`TurnInputContributor`（per-turn 上下文注入）、`ToolContributor`（声明工具）、`ToolLifecycleContributor`（on_tool_start/finish）、`TurnLifecycleContributor`（on_turn_start/stop/abort）、`ThreadLifecycleContributor`（on_thread_start/stop）、`ConfigContributor`（on_config_changed）、`ApprovalReviewContributor`（权限覆盖复核，approval 实际走这里）。
  - Runtime Service ABC（`capabilities.py`，构造注入给 extension）：`SchedulerService`（有实现 `runtime.scheduler.FileSchedulerService`）、`AgentRunner`（同步跑子 agent 取回最终文本，实现 `agent_adapter.agent_runner.GatewayAgentRunner`，deep_research 用）。
  - 组装：`ExtensionRegistryBuilder().add(ext).build()` → frozen `ExtensionRegistry`，由 `boot(extension_registry=...)` 注入 `DefaultHarness`；`None` 由 boot 统一组装默认扩展，不另走旧执行路径。
- [evolution_module](evolution_module.md) — 自进化迭代实现：`src/xana/evolution/`（账本 / 变更单 / episode 状态机 / 元 agent 服务）。
- session_config — per-session 运行时旋钮（model/effort/streaming 等）持久化边界（`src/xana/session_config/`），尚无模块文档。

## 设计方向

- [workbench](workbench.md) — 已实现的主/子会话分页查询、活动事件、浏览器同源认证与断线恢复；Web 页面等仍按实施计划推进。

- [evolution](evolution.md) — 24h 自进化迭代框架设计共识（status: active；四个阶段已全部落地，机制真相见 [evolution_module](evolution_module.md)，本文只保留远期方向）。
- [project 主 agent 与可复用能力](../docs/design/project-capability-design.md) — 已记录 Web 为主、聊天为辅的用户方向，以及独立工作区、可发布运行核、特殊迭代 skills 和压缩来源边界；迁移接口与实现仍是 proposed，现有服务契约继续有效。
- [Kimi Web 源码核查与首版范围](../docs/design/project-capability-design.md#kimi-code-web-源码核查2026-11-09) — 三档会话权限、持久 goal/checklist、运行中子会话查看、日志与打断；排除 swarm/tower，含固定源码版本与依据。
- [Web 工作台实施计划](../docs/design/web-workbench-implementation-plan.md) — 分阶段契约、数据迁移、旧路径删除与验收；新建会话默认必要时询问；源码实施与隔离验收进行中，尚未上线。
- [Codex 插话机制源码审阅](../exp_ai/context_codex_steering.md) — 后续实现参照：单一活动循环、插话队列、等待与任务取消分离、已完成工作保留及迁移验收；含源码版本与测试证据；当前实现边界见单循环迁移契约。
- [后台任务机制源码调研](../exp_ai/harness_background_task_review.md) — Codex、Claude Code、Kimi Code 的等待、完成通知与空闲唤醒机制，及 Xana 后续完善建议；status: reference，尚未实施。
- [单循环对话与取消迁移](../docs/design/single-loop-conversation-migration.md) — 单循环实现、离线数据迁移与验收路径。
- [单循环迁移验收](../docs/design/single-loop-conversation-acceptance.md) — 测试、真实数据副本验证、生产切换及回滚备份。

## 命名规则

- `module.md`：模块设计，说明职责、输入输出、边界。
- `module_topic.md`：细节设计，按子模块/类职责拆分，例如 `memory_tree.md`、`memory_search_engine.md`。
- `exp_ai/*.md`：经验和迁移记录，可以挂到某个模块或 detail 上。

## TODO 关系

- [todo_ai](../todo_ai/README_ai.md) — 特殊记忆树，只维护待办；设计变更必须先更新 `doc_ai`。
