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
- [agents_request_mapping](agents_request_mapping.md) — Map AgentRequest to HarnessRequest and HarnessContext.
- [agents_subagents](agents_subagents.md) — Subagent dispatch contract and delegation result shape.
- [agents_registry](agents_registry.md) — Agent registration and spec listing；含 `FileAgentRegistry`（AGENT.md 文件加载）。

## 文件 Agent 文档（AGENT.md，计划 §5.1）

用户可在 `{XANA_HOME}/agents/{name}/AGENT.md`（默认 `~/.xana/agents/`）放角色定义文档：frontmatter 必填 `kind: agent` / `name` / `goal`，可选 `when_to_use`（何时用它，进可委派列表）/ `tools`（工具选择表达式：不写 = 默认组合，`[]` = 零工具，可写组名 / 工具名 / `*`）/ `disallowed_tools` / `skills` / `budget` / `stop_rules` / `output_contract` / `prompt_profile`（模式名或开关 mapping）/ `verify`（检查命令列表）；正文即系统提示词模板（含 `${base_prompt}` 在默认提示词上扩展，不含则完全替换），字段映射见 [agents_spec](agents_spec.md)，设计见 [agent-composition-design](../docs/design/agent-composition-design.md)。boot 时经 `FileAgentRegistry` 加载为 `ChatAgent` 实例注册进 `gateway.agents`，builtin chat 保持默认。

## 切换、委派与创建

设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md)。

- **切换**：会话在创建时绑定 agent（`SessionConfig.agent`，`None` = 默认 chat），之后不切换。`/agent <名字>` 换一个新会话 id 并绑定该 agent，旧会话原样保留；`/clear` 把绑定带到新会话；router 据此决定 `target_agent`（见 [agent_adapter_commands](agent_adapter_commands.md)）。
- **委派**：`agent_run` 工具经 `AgentRunner` 在独立子会话里同步跑另一个 agent、取回最终文本，可并行、可 `resume` 续接；子 agent 拿不到 `agent_run`，深度固定为 1（见 [agents_subagents](agents_subagents.md)）。
- **创建**：`agent_define` 工具（`catalog` 看可选工具组 / `create` 写 `~/.xana/agents/<name>/AGENT.md`）配合内置 `agent_creator` 技能（访谈 → 起草 → 用户在对话中确认 → create）；写入后经 `FileAgentRegistry.load_file` 与 boot 同一路径解析并注册进 `gateway.agents`，立即可用。新 agent 的工具不能超出调用方本 run 可用的工具，不能覆盖内置 agent；`~/.xana/agents` 对 Write / Edit 受保护，`agent_define` 是唯一写入口。

另有内置 `researcher`（`agents/researcher.py`）：deep_research 派出的子研究员，工具 `("web", "Read")`（web 组 + Read）、提示词 `${base_prompt}` + 研究员身份、max_turns 20、不向用户提问；配置了 xana-search 时由 boot 注册（不接进度汇报，不触碰渠道），不进文件 agent 注册表。

## 与 SKILL.md 的边界

AGENT.md 与 SKILL.md 共用同一个解析器（`MarkdownDocParser` 按 frontmatter `kind` 区分），但物理目录分开（`~/.xana/agents/` vs `~/.xana/skills/`），语义完全不同：**skill 是做法库**——prompt 指令包，经 `skill_load` 两阶段按需取全文，不 spawn 任何实体；**agent 是角色定义**——可被 spawn 的 AgentSpec 来源（系统提示词模板 + 工具选择 + 技能名单）。一个 agent 的 frontmatter `skills` 字段引用的是 skill 名（映射到 `allowed_skills`，目前无消费者）。详见 [skills](skills.md) 与 [skills_package](skills_package.md)。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Agent identity, system prompt template, allowed tools/skills, outer chat/control API, and session-facing behavior.

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
