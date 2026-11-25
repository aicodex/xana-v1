---
title: Module IO Map
type: doc
status: active
---

# Module IO Map

| Module | Primary Input | Primary Output | Owns | Must Not Own |
| --- | --- | --- | --- | --- |
| protocol | 纯数据值、磁带事件、评测试次 | 规范 JSON、Tape 包（`manifest.json` + `tape.jsonl` + `blobs/`）、`BenchmarkSnapshot` | 基础值原语、双版本序列化、Tape/Eval 协议（纯 stdlib，零向上依赖） | 业务接口、运行时状态 |
| component | 组件源元数据 | `ComponentSpec` / `ComponentSource` / `LoadError` | 共享组件元数据合约 | 加载与注册表实现 |
| multimodal | 媒体文件与元数据 | `MediaInput` / `InputModality` / 内容寻址媒体存储 | 媒体输入契约与资产校验存储 | 渠道发送协议 |
| config | project root, `~/.xana/config/system.json` + `projects/<id>/config.json` | `RuntimeConfig` / `SystemConfig`、路径解析（`config/paths.py`） | config merge（用户级 < 项目级，项目只能改 `PROJECT_KEYS`，mcp 段按 name 索引的专门合并见 [mcp](mcp.md)） | runtime state |
| policy | `AuthorizationRequest` / `ProcessSpec` / `PathOperation` | `PermissionResult` / `Approval` / `ProcessOutcome` + `policy.*`/`env.*` Tape | 会话权限（manual/yolo/auto）、plan mode、审批、路径门禁、进程组隔离执行环境（`python -m xana.policy`） | 工具业务实现、会话/配置存储实现 |
| state | session/run/todo/budget/process_log 写入 | `SessionRecord` / `RunRecord` / `UsageSnapshot` / 启动恢复扫描 | 会话、run、预算、TODO、进程日志持久化与孤儿扫描 | 模型调用与主循环 |
| mcp | MCP server 配置（用户/项目两层 + override）、官方 SDK 连接、`os.environ` 凭据展开 | 已审核并发布的 MCP 工具（共享 `FileToolRegistry`）、脱敏 `ServerView`/`ToolView`、`mcp/audit.jsonl` | 连接/退避/重审状态机、工具审核与发布、`/mcp*` 管理 API | 工具执行编排、权限决策、配置模型定义 |
| runtime | project root, `ConfigProvider`, `extension_registry` | `BootResult`、daemon HTTP（`/healthz`） | 唯一组合根 `boot()`、进程生命周期、项目管理（`runtime/projects.py`）、调度与投递 | agent/tool behavior |
| harness_sdk | `HarnessRequest` | `HarnessEvent` stream | turn lifecycle、单循环控制、压缩、goal/background | protocol rendering |
| agents | `AgentRequest` | `AgentEvent` stream | agent spec、outer API、委派组合与版本快照 | turn loop |
| tools | `ToolInvocation` | `ToolResult` | tool execution boundary、内置与派生工具、评测 `ToolEnvironment` | memory/session state |
| skills | `SkillActivation` | `SkillContribution` | context/prompt contribution、技能包发现与管理 | tool execution |
| agent_adapter | external message | `InboundMessage` / rendered reply | protocol translation（CLI/TG/Feishu/DaemonBridge）与卡片渲染 | agent logic |
| llm_driver | `ModelRequest` + `ProviderProfile` + `CredentialResolver` + `PolicyBundle` | `ModelEvent` stream / `ModelOutcome` + `llm.*` Tape 事件 | 线协议传输（OpenAI Chat / Anthropic Messages）、错误分类、重试/恢复/改写/路由策略、回放与故障注入（`python -m xana.llm_driver`） | tool dispatch、会话状态、config 路径解析 |
| memory | context/history rows、项目记忆读写与检索请求 | `ActiveContext` / replay result / `InjectedBlock` / `MemorySnapshot` / `RetrievalPlan` | durable conversation log + 项目记忆存储（支持快照/分叉/回滚）与 SAG2 + Karpo 4B 检索召回（`python -m xana.memory`） | agent decisions |
| context | append/replace/snapshot 请求（sid, agent） | Message buffer / 序列化消息 | active context 运行时载体（ContextSDK） | 持久化、检索索引 |
| extension_api | extension 对象 | contributor 调用点（prompt/turn input/tools/lifecycle/config/approval review）+ runtime service（`SchedulerService` / `AgentRunner`） | extension 合约与 frozen registry | 具体 agent/tool 行为 |
| session_config | slash-command 变更 | `SessionConfig` 持久化（get/save/delete） | per-session 运行时旋钮持久化边界 | mid-turn 配置变更 |
| evolution | 评测套件、候选版本、预算配置 | `EvalRunner` 批次结果、`EpisodeRunner` 状态机、append-only `Ledger` | 离线评测运行、预算收尾、canary 与回滚账本 | 生产会话主循环 |
| benchmarks | 外部基准任务（Harbor / τ²-bench） | 隔离工作区运行结果与评测输出 | 官方 benchmark 独立运行器适配 | 生产项目状态 |
| workbench | 会话/run/媒体查询请求 | 分页会话投影与媒体上传视图 | Web 工作台只读投影与上传服务 | 独立执行状态机 |
| publishing | Agent 目录与依赖声明 | `AgentPackage` 安装包与独立启动器 | Agent 打包、校验、安装与启动 | 生产守护进程循环 |
| cli | CLI args（`--project <id>`）, 合并后的 system.json + 项目 config.json, daemon endpoint | `~/.xana/` 目录树（含 `projects/<id>/`）、每项目一个 serve 进程、stdout/xanac 卡片 | 命令入口、安装引导、旧布局迁移、项目命令、配对管理、daemon 客户端 | agent/harness/adapter 业务逻辑 |
