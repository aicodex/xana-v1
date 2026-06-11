---
title: agents
type: doc
status: active
---

# agents

## 定位

agent spec、outer API、会话侧行为包装。

## 详细设计

- [agents_spec](agents_spec.md) — Agent identity, model, tools, skills, permissions, memory scope.
- [agents_prompt_provider](agents_prompt_provider.md) — Agent prompt blocks: identity, instructions, memory.
- [agents_request_mapping](agents_request_mapping.md) — Map AgentRequest to HarnessRequest and HarnessContext.
- [agents_subagents](agents_subagents.md) — Subagent dispatch contract and delegation result shape.
- [agents_registry](agents_registry.md) — Agent registration and spec listing.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Agent identity, prompt provider selection, allowed tools/skills, outer chat/control API, and session-facing behavior.

## Does Not Own

Harness turn loop, adapter protocol rendering, low-level LLM calls, or memory persistence format.

## Inputs

- `AgentRequest`
- `AgentControlRequest`
- session state
- harness SDK

## Outputs

- `AgentEvent` stream
- control results
- session updates through the configured store
