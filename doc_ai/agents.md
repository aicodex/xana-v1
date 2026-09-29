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
- [agents_registry](agents_registry.md) — Agent registration and spec listing；含 `FileAgentRegistry`（两级目录 AGENT.md 热加载、删除）。

## 文件 Agent 文档（AGENT.md，计划 §5.1）

角色定义文档放在两级目录（同名时项目级覆盖用户级，内置 chat / researcher 不能被覆盖）：用户级 `{XANA_HOME}/agents/{name}/AGENT.md`（默认 `~/.xana/agents/`，所有项目通用）、项目级 `<project_root>/.xana/agents/{name}/AGENT.md`（本 serve 所属项目）。frontmatter 必填 `kind: agent` / `name` / `goal`，可选 `when_to_use`（何时用它，进可委派列表）/ `tools`（工具选择表达式：不写 = 项目 `agent_defaults.tools`（缺省 default 组合），`[]` = 零工具，可写组名 / 工具名 / `*`）/ `disallowed_tools` / `skills`（技能选择：不写 = 项目 `agent_defaults.skills`（缺省全部），`[]` = 无）/ `budget` / `stop_rules` / `output_contract` / `prompt_profile`（模式名或开关 mapping）/ `verify`（检查命令列表）；正文即系统提示词模板（含 `${base_prompt}` 在默认提示词上扩展，不含则完全替换），字段映射见 [agents_spec](agents_spec.md)，设计见 [agent-composition-design](../docs/design/agent-composition-design.md)。`FileAgentRegistry` 直接维护 `gateway.agents` 表并热加载：增、改、删 AGENT.md 不需要重启 serve（gateway 路由前、`agent_run`、可委派列表按 2 秒节流刷新，`/agent` 与 `agent_define` 立即刷新），builtin chat 保持默认（通用提示词，不带编码规则；写代码时加载 `coding` 技能，见 [harness_prompt_assembly](harness_prompt_assembly.md)）。设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md) §7。

## 切换、委派与创建

设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md)。

- **切换**：会话在创建时绑定 agent（`SessionConfig.agent`，`None` = 默认 chat），之后不切换。`/agent <名字>` 换一个新会话 id 并绑定该 agent，旧会话原样保留；`/clear` 把绑定带到新会话；router 据此决定 `target_agent`（见 [agent_adapter_commands](agent_adapter_commands.md)）。
- **委派**：`agent_run` 工具经 `AgentRunner` 在独立子会话里同步跑另一个 agent、取回最终文本，可并行、可 `resume` 续接；子 agent 拿不到 `agent_run`，深度固定为 1（见 [agents_subagents](agents_subagents.md)）。
- **创建 / 删除**：`agent_define` 工具（`catalog` 看可选工具组 / `create` 按 `scope` 写 `<project_root>/.xana/agents/<name>/AGENT.md`（`project`，缺省）或 `~/.xana/agents/<name>/AGENT.md`（`user`）/ `delete` 删除文件 agent）配合内置 `agent_creator` 技能（访谈 → 起草 → 用户在对话中确认 → create）；写入 / 删除后 `FileAgentRegistry.refresh(force=True)` 立即生效。新 agent 的工具不能超出调用方本 run 可用的工具，不能覆盖或删除内置 agent；两级 agent 目录对 Write / Edit 受保护，`agent_define` 是唯一写入口。
- **删除 / 修改对已有会话的影响**：绑定的 agent 被删除后，下一条消息 gateway 明确报错「agent X 不存在(可能已删除),用 /agent 选择其他 agent」，不静默改用 chat；被修改时新会话按新定义，已有会话的系统提示词在会话内固定，工具等其他字段从下一条消息起按新定义生效。

另有内置 `researcher`（`agents/researcher.py`）：deep_research 派出的子研究员，工具 `("web", "Read")`（web 组 + Read）、提示词 `${base_prompt}` + 研究员身份、max_turns 20、不向用户提问；配置了 xana-search 时由 boot 注册（不接进度汇报，不触碰渠道），不进文件 agent 注册表。

## 与 SKILL.md 的边界

AGENT.md 与 SKILL.md 共用同一个解析器（`MarkdownDocParser` 按 frontmatter `kind` 区分），但物理目录分开（`~/.xana/agents/` vs `~/.xana/skills/`），语义完全不同：**skill 是做法库**——prompt 指令包，经 `skill_load` 两阶段按需取全文，不 spawn 任何实体；**agent 是角色定义**——可被 spawn 的 AgentSpec 来源（系统提示词模板 + 工具选择 + 技能名单）。一个 agent 的 frontmatter `skills` 字段引用的是 skill 名（映射到 `AgentSpec.skills`，决定技能可见性：技能列表只列可见技能，`skill_load` 拒绝不可见技能）。详见 [skills](skills.md) 与 [skills_package](skills_package.md)。

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
