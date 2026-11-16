---
title: Module IO Map
type: doc
status: active
---

# Module IO Map

| Module | Primary Input | Primary Output | Owns | Must Not Own |
| --- | --- | --- | --- | --- |
| config | project root, `~/.xana/config/system.json` + `projects/<id>/config.json` | `RuntimeConfig` / `SystemConfig`、路径解析（`config/paths.py`） | config merge（用户级 < 项目级，项目只能改 `PROJECT_KEYS`，mcp 段按 name 索引的专门合并见 [mcp](mcp.md)） | runtime state |
| mcp | MCP server 配置（用户/项目两层 + override）、官方 SDK 连接、`os.environ` 凭据展开 | 已审核并发布的 MCP 工具（共享 `FileToolRegistry`）、脱敏 `ServerView`/`ToolView`、`mcp/audit.jsonl` | 连接/退避/重审状态机、工具审核与发布、`/mcp*` 管理 API | 工具执行编排、权限决策、配置模型定义 |
| runtime | project root, `ConfigProvider`, `extension_registry` | `BootResult`、daemon HTTP（`/healthz`） | process lifecycle；项目注册 / 查询 / 启停（`runtime/projects.py`） | agent/tool behavior |
| harness_sdk | `HarnessRequest` | `HarnessEvent` stream | turn lifecycle | protocol rendering |
| agents | `AgentRequest` | `AgentEvent` stream | agent behavior wrapper、subagent 派发（`GatewaySubagentDispatcher`） | turn loop |
| tools | `ToolInvocation` | `ToolResult` | tool execution boundary | memory/session state |
| skills | `SkillActivation` | `SkillContribution` | context/prompt contribution | tool execution |
| agent_adapter | external message | `InboundMessage` / rendered reply | protocol translation | agent logic |
| llm_driver | `ModelRequest` | `ModelEvent` stream | provider IO | tool dispatch |
| memory | context/history rows | `ActiveContext` / replay result | durable conversation log + replayable runtime view | agent decisions |
| context | append/replace/snapshot 请求（sid, agent） | Message buffer / 序列化消息 | active context 运行时载体（ContextSDK） | 持久化、检索索引 |
| extension_api | extension 对象 | contributor 调用点（prompt/turn input/tools/lifecycle/config/approval review）+ runtime service（SchedulerService / AgentSpawner / AgentRunner，均已有实现） | extension 合约与 frozen registry | 具体 agent/tool 行为 |
| session_config | slash-command 变更 | `SessionConfig` 持久化（get/save/delete） | per-session 运行时旋钮持久化边界 | mid-turn 配置变更 |
| cli | CLI args（`--project <id>`）, 合并后的 system.json + 项目 config.json, daemon endpoint | `~/.xana/` 目录树（含 `projects/<id>/`）、每项目一个 serve 进程、stdout/xanac 卡片 | 命令入口、安装引导、旧布局迁移、项目命令、配对管理、daemon 客户端 | agent/harness/adapter 业务逻辑 |
