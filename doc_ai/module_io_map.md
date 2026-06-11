---
title: Module IO Map
type: doc
status: active
---

# Module IO Map

| Module | Primary Input | Primary Output | Owns | Must Not Own |
| --- | --- | --- | --- | --- |
| config | project root, config files | `RuntimeConfig` | config merge | runtime state |
| runtime | `BootRequest` | `RuntimeHealth` | process lifecycle | agent/tool behavior |
| harness_sdk | `HarnessRequest` | `HarnessEvent` stream | turn lifecycle | protocol rendering |
| agents | `AgentRequest` | `AgentEvent` stream | agent behavior wrapper | turn loop |
| tools | `ToolInvocation` | `ToolResult` | tool execution boundary | memory/session state |
| skills | `SkillActivation` | `SkillContribution` | context/prompt contribution | tool execution |
| agent_adapter | external message | `InboundMessage` / rendered reply | protocol translation | agent logic |
| llm_driver | `ModelRequest` | `ModelEvent` stream | provider IO | tool dispatch |
| memory | context/history rows | `ActiveContext` / replay result | runtime context authority + mirror | agent decisions |
| observability | trace/debug request | trace/snapshot | display and diagnostics | state authority |
