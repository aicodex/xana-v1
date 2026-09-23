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
- [agents_registry](agents_registry.md) — Agent registration and spec listing；含 `FileAgentRegistry`（AGENT.md 文件加载）。

## 文件 Agent 文档（AGENT.md，计划 §5.1）

用户可在 `{XANA_HOME}/agents/{name}/AGENT.md`（默认 `~/.xana/agents/`）放角色定义文档：frontmatter 必填 `kind: agent` / `name` / `goal` / `tools`（白名单），可选 `skills` / `budget` / `stop_rules` / `output_contract`；正文是工作流步骤，渲染进 AgentSpec.identity。boot 时经 `FileAgentRegistry` 加载为 `ChatAgent` 实例注册进 `gateway.agents`，builtin chat 保持默认。

## 与 SKILL.md 的边界

AGENT.md 与 SKILL.md 共用同一个解析器（`MarkdownDocParser` 按 frontmatter `kind` 区分），但物理目录分开（`~/.xana/agents/` vs `~/.xana/skills/`），语义完全不同：**skill 是做法库**——prompt 指令包，经 `skill_load` 两阶段按需取全文，不 spawn 任何实体；**agent 是角色定义**——可被 spawn 的 AgentSpec 来源（identity + 工具/技能白名单）。一个 agent 的 frontmatter `skills` 字段引用的是 skill 名（映射到 `allowed_skills`，目前无消费者）。详见 [skills](skills.md) 与 [skills_package](skills_package.md)。

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
