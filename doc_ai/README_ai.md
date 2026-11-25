---
title: doc_ai
type: doc
status: active
---

# Xana 架构导航（`doc_ai`）

`doc_ai` 是 Xana 当前实现的架构真相。Xana 按 **底座（L0）+ 7 个独立可进化核心层（L1–L7）** 组织，每层按目录隔离、可独立运行离线 `bench`、有黄金契约锁（`tests/golden/*_contract_v1.json`）、支持 `XANA_DIAG` 按标签开关诊断日志，并统一在唯一组合根 [runtime/boot.py](../src/xana/runtime/boot.py) 装配。

> 接口签名速查：[modular_interfaces.md](modular_interfaces.md) ｜ 输入输出总表：[module_io_map.md](module_io_map.md) ｜ 系统数据流：[system.md](system.md) ｜ 架构依赖门禁：[test_arch_layers.py](../tests/test_arch_layers.py)

---

## 一、核心 7 层（+ L0 观测与协议底座）

| 层级 | 目录 | 职责 | 如何接入（契约 / 装配 / 独立入口） | 详细文档 |
|---|---|---|---|---|
| **L0 协议与观测底座** | `src/xana/protocol/` | 零业务依赖纯 stdlib 原语、双版本序列化、Append-Only `TapeWriter`（含 CAS Blob）、统一 `EvalTrialRecord`/`BenchmarkSnapshot`、可开关诊断日志 `DiagLogger` | 所有上层包均可单向依赖；`XANA_DIAG=<tag>` 开诊断日志；`XANA_TAPE_DIR=<dir>` 在 `boot()` 开启全链路 Tape 落盘；独立 CLI：`python -m xana.protocol tape ...` | [protocol.md](protocol.md) |
| **L1 模型驱动** | `src/xana/llm_driver/` | 多 Provider 线协议传输（OpenAI Chat / Anthropic Messages / Kimi OAuth）、错误分类、流式事件归一化、重试/恢复/改写/路由策略 | 经 `ModelClient`/`ModelRegistry` + 4 个策略 ABC 接入，由 `boot()` 构造并注入 `DefaultHarness`；标签 `XANA_DIAG=llm`；独立 CLI：`python -m xana.llm_driver bench` | [llm_driver.md](llm_driver.md) |
| **L2 权限与执行环境** | `src/xana/policy/` | 会话权限模式（manual/yolo/auto）、Plan Mode、审批流、命令风险分级、路径写保护门禁、进程组隔离执行环境 | 通过 `PolicyStatePort`/`PolicyEventSink` 适配宿主（[runtime/policy_adapter.py](../src/xana/runtime/policy_adapter.py)），由 `boot()` 装配 `PermissionService` 与 `LocalProcessEnvironment`；标签 `XANA_DIAG=policy`；独立 CLI：`python -m xana.policy bench` | [policy.md](policy.md) |
| **L3 工具与观测管理** | `src/xana/tools/` | 工具校验→权限→执行生命周期、批处理调度（`batches.py`）、工具输出归一化/截断/污染标记（`ObservationPolicy`）、按需揭示（`ToolDisclosurePolicy`）、内置与派生工具 | 实现 `Tool` ABC 注册到 `FileToolRegistry`，策略槽与 `ToolTapeRecorder` 构造注入 `DefaultHarness`；标签 `XANA_DIAG=tools`；独立 CLI：`python -m xana.tools bench` | [tools.md](tools.md) |
| **L4 技能** | `src/xana/skills/` | 技能包发现/加载/管理、目录渲染与预算裁剪（`SkillCatalogPolicy`）、候选技能提案队列（`SkillProposalQueue`） | 经 `SkillsListingContributor`（`extension_api/builtin/`）注入提示词、经 `SkillLoadTool` 按需加载；提案队列挂在 `BootResult.skill_proposals`；标签 `XANA_DIAG=skills`；独立 CLI：`python -m xana.skills bench` | [skills.md](skills.md) |
| **L5 长期记忆（SAG2）** | `src/xana/memory/` | 项目权威 Markdown 记忆（`memory/*.md`，支持 `snapshot`/`fork`/`rollback`）、**SAG2 + 本地 Karpo 4B** 证据评分检索与召回（`MemoryRetrievalPolicy`）、后台记忆提取（`MemoryExtractionPolicy`） | 经 `MemoryStore`/`MemorySearchEngine`/`MemoryInjector` 接入；L1 自动召回与 L2 `memory_search` 共用同一 `ProjectMemorySearch` 实例，`memory_write` 为唯一写入口；标签 `XANA_DIAG=memory`；独立 CLI：`python -m xana.memory bench` | [memory.md](memory.md)、[memory_sag2.md](memory_sag2.md)、[memory_extract.md](memory_extract.md) |
| **L6 Harness 与上下文** | `src/xana/harness_sdk/`<br>`src/xana/context/` | 单会话单循环（queue/steer/cancel）、固定前缀 Prompt 组装、原子前缀压缩（`ContextSDK` + `CompactionPolicy`）、回合收尾与续跑熔断（`StopDecisionPolicy`）、Goal/TODO/后台任务 | `DefaultHarness` 接收所有策略槽与 `ExtensionRegistry`，对外暴露 `Harness.invoke/compact/cancel` 有序事件流；标签 `XANA_DIAG=harness,context`；独立 CLI：`python -m xana.harness_sdk bench` | [harness_sdk.md](harness_sdk.md)、[context.md](context.md)、[harness_background_tasks.md](harness_background_tasks.md) |
| **L7 评测与自进化** | `src/xana/evolution/` | 评测套件执行与确定性打分（`BenchmarkRunner`）、配对 train/keep/heldout 防过拟合晋升门禁（`PromotionGate`）、Append-Only 迭代账本（`IterationLedgerStore`）、Episode 预算状态机与回滚 | 经 `EvalRunner`/`Ledger`/`StatisticalPromotionGate` 在 `boot()` 装配，通过 `eval_*` 与 `ledger` 工具暴露给迭代 Agent；标签 `XANA_DIAG=eval`；独立 CLI：`python -m xana.evolution bench` | [evolution_module.md](evolution_module.md) |

---

## 二、其他目录说明（宿主装配、渠道、状态与支撑）

### 1. 宿主装配与扩展边界
- `src/xana/runtime/` — **唯一组合根** `boot()`、进程生命周期、项目管理（`runtime/projects.py`）、定时调度与结果投递、daemon HTTP、SAG2 评分/提取宿主实现（`memory_judge.py`、`memory_extract.py`）、策略宿主适配（`policy_adapter.py`）。见 [runtime.md](runtime.md)。
- `src/xana/extension_api/` — 冻结扩展规约：8 种 Contributor ABC（`interfaces.py`）、Runtime Service ABC（`SchedulerService` / `AgentRunner` in `capabilities.py`）、`ExtensionRegistryBuilder` 组装。
- `src/xana/agents/` — Agent 规约（`AGENT.md`）、外层 `ChatAgent` 包装、子 Agent 委派组合、版本快照与内置迭代者（`iterator`）。见 [agents.md](agents.md)。

### 2. 渠道、CLI 与前端工作台
- `src/xana/agent_adapter/` — 外部渠道协议翻译层：CLI/daemon 桥接、飞书（WebSocket 长连接 + 卡片）、Telegram、统一卡片状态机、`DefaultAgentGateway` 与命令路由。见 [agent_adapter.md](agent_adapter.md)。
- `src/xana/cli/` — 命令行入口：`xana`（`setup`/`sync`/`serve`/`pair`/`project`/`login`/`logout`/`mcp`）与 `xanac` 交互客户端。见 [cli.md](cli.md)。
- `src/xana/workbench/` — Web 工作台只读会话/Run 投影、活动事件流与媒体上传服务。见 [workbench.md](workbench.md)。

### 3. 外部协议、配置、状态与共享契约
- `src/xana/mcp/` — 外部 MCP Server 配置、官方 SDK 连接、工具审核（`audit.jsonl`）、热加载与发布状态机。见 [mcp.md](mcp.md)。
- `src/xana/config/` — 两层配置加载（用户级 `~/.xana/config/system.json` < 项目级 `projects/<id>/config.json`）与唯一路径解析点（`paths.py`）。见 [config.md](config.md)。
- `src/xana/state/` — 会话（`SessionStore`）、运行（`RunStore` / `RunJsonlWriter`）、预算、TODO、进程日志持久化与启动孤儿扫描。见 [state.md](state.md)。
- `src/xana/session_config/` — Per-session 运行时旋钮（model / effort / streaming / permission mode）持久化边界。
- `src/xana/component/` — 共享组件元数据契约（`ComponentSpec`、`ComponentSource`、`LoadError`）。见 [component.md](component.md)。
- `src/xana/multimodal/` — 多模态媒体输入契约（`MediaInput`、`InputModality`）与内容寻址媒体存储。

### 4. 外部基准与打包发布
- `src/xana/benchmarks/` — 官方外部基准（Terminal-Bench / Harbor、τ²-bench）的独立运行器与隔离工作区。见 [benchmarks.md](benchmarks.md)。
- `src/xana/publishing/` — Agent 目录打包、依赖校验、独立安装与启动器生成。

---

## 三、测试与文档约定

- **测试目录（`tests/`，独立仓库）**：按 7 层各自建子目录（`tests/{protocol,llm_driver,policy,tools,skills,memory,harness_sdk,evolution}/`），黄金契约锁在 `tests/golden/*_contract_v1.json`，跨包架构门禁在 `tests/test_arch_layers.py`，真机/卡片门禁在 `tests/e2e_real/`（`-m merge_gate`）。
- **设计参考与规范**：
  - [replacement_rule.md](replacement_rule.md) — 实现替换规则
  - [io_contract_template.md](io_contract_template.md) — 新合约文档模板
  - [sdk-migration-roadmap.md](../docs/design/sdk-migration-roadmap.md) — Stage 0–7 模块化迁移落地记录（已完成）
  - [project-design.md](../docs/design/project-design.md) — 项目隔离与数据目录布局
  - [todo_ai](../todo_ai/README_ai.md) — 待办记忆树（设计变更须先更新 `doc_ai`）
