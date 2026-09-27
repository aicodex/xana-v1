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

## 核心模块

- [config](config.md) — 配置加载、系统/项目/agent 覆盖规则。
- [runtime](runtime.md) — 进程生命周期、启动顺序、调度外壳。
- [harness_sdk](harness_sdk.md) — turn loop、暂停/恢复/取消、事件流和 usage 快照。
- [agents](agents.md) — agent spec、outer API、会话侧行为包装。
- [tools](tools.md) — 工具校验、权限、沙箱、执行边界和输出 schema。
- [skills](skills.md) — 技能元数据、激活规则、上下文/提示注入。
- [agent_adapter](agent_adapter.md) — CLI/TG/Feishu/HTTP 等外部协议翻译。
- [cli](cli.md) — 命令分发：`xana` 管理命令（setup/sync/serve/pair/login/logout）与 `xanac` 交互客户端。
- [llm_driver](llm_driver.md) — 模型请求、流式响应、tool call 解析、usage 提取。
- [memory](memory.md) — active context 权威、history mirror、treemem/search/injection。
- [context](context.md) — active context 的运行时载体：`ContextSDK` facade、per-sid 锁、compact-as-placeholder。

## 支撑模块

- [component](component.md) — 共享组件元数据合约（`ComponentSpec` / `ComponentSource`）。
- [policy](policy.md) — 权限、沙箱、approval 数据合约。
- [state](state.md) — session、run、checkpoint、resume、usage 状态合约。
- extension_api — extension 合约（`src/xana/extension_api/`），尚无模块文档。
  - 8 种 Contributor ABC（`interfaces.py`）：`ContextContributor`（system prompt 片段）、`TurnInputContributor`（per-turn 上下文注入）、`ToolContributor`（声明工具）、`ToolLifecycleContributor`（on_tool_start/finish）、`TurnLifecycleContributor`（on_turn_start/stop/abort）、`ThreadLifecycleContributor`（on_thread_start/stop）、`ConfigContributor`（on_config_changed）、`ApprovalReviewContributor`（权限覆盖复核，approval 实际走这里）。
  - Runtime Service ABC（`capabilities.py`，构造注入给 extension）：`SchedulerService`（有实现 `runtime.scheduler.FileSchedulerService`）、`AgentSpawner`（首个实现 `agent_adapter.gateway.GatewayAgentSpawner`，spawn 链 + fork 前缀拷贝）、`AgentRunner`（同步跑子 agent 取回最终文本，实现 `agent_adapter.agent_runner.GatewayAgentRunner`，deep_research 用）。
  - 组装：`ExtensionRegistryBuilder().add(ext).build()` → frozen `ExtensionRegistry`，由 `boot(extension_registry=...)` 注入 `DefaultHarness`；`None` 时行为与未引入 Extension API 前一致。
- [evolution_module](evolution_module.md) — 自进化迭代实现：`src/xana/evolution/`（账本 / 变更单 / episode 状态机 / 元 agent 服务）。
- session_config — per-session 运行时旋钮（model/effort/streaming 等）持久化边界（`src/xana/session_config/`），尚无模块文档。

## 设计方向

- [evolution](evolution.md) — 24h 自进化迭代框架设计共识（status: active；四个阶段已全部落地，机制真相见 [evolution_module](evolution_module.md)，本文只保留远期方向）。

## 命名规则

- `module.md`：模块设计，说明职责、输入输出、边界。
- `module_topic.md`：细节设计，按子模块/类职责拆分，例如 `memory_tree.md`、`memory_search_engine.md`。
- `exp_ai/*.md`：经验和迁移记录，可以挂到某个模块或 detail 上。

## TODO 关系

- [todo_ai](../todo_ai/README_ai.md) — 特殊记忆树，只维护待办；设计变更必须先更新 `doc_ai`。
