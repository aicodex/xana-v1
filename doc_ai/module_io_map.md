---
title: Module IO Map
type: doc
status: active
---

# Module IO Map

| Module | Primary Input | Primary Output | Owns | Must Not Own |
| --- | --- | --- | --- | --- |
| config | project root, config files | `RuntimeConfig` | config merge | runtime state |
| runtime | project root, `ConfigProvider`, `extension_registry` | `BootResult`、daemon HTTP（`/healthz`） | process lifecycle | agent/tool behavior |
| harness_sdk | `HarnessRequest` | `HarnessEvent` stream | turn lifecycle | protocol rendering |
| agents | `AgentRequest` | `AgentEvent` stream | agent behavior wrapper、subagent 派发（`GatewaySubagentDispatcher`） | turn loop |
| tools | `ToolInvocation` | `ToolResult` | tool execution boundary | memory/session state |
| skills | `SkillActivation` | `SkillContribution` | context/prompt contribution | tool execution |
| agent_adapter | external message | `InboundMessage` / rendered reply | protocol translation | agent logic |
| llm_driver | `ModelRequest` | `ModelEvent` stream | provider IO | tool dispatch |
| memory | context/history rows | `ActiveContext` / replay result | runtime context authority + mirror | agent decisions |
| context | append/replace/snapshot 请求（sid, agent, run_id） | Message buffer / 序列化消息 | active context 运行时载体（ContextSDK） | 持久化、检索索引 |
| extension_api | extension 对象 | contributor 调用点（prompt/turn input/tools/lifecycle/config/approval review）+ runtime service（SchedulerService / AgentSpawner / AgentRunner，均已有实现） | extension 合约与 frozen registry | 具体 agent/tool 行为 |
| session_config | slash-command 变更 | `SessionConfig` 持久化（get/save/delete） | per-session 运行时旋钮持久化边界 | mid-turn 配置变更 |
| cli | CLI args, `~/.xana/config/system.json`, daemon endpoint | `~/.xana/` 目录树、serve 进程、stdout/xanac 卡片 | 命令入口、安装引导、配对管理、daemon 客户端 | agent/harness/adapter 业务逻辑 |
