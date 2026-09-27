---
title: skills
type: doc
status: active
---

# skills

## 定位

技能（skill）的元数据、注册发现与激活合约，以及「列表注入 + 按需加载」的两阶段提示注入模式。技能本体是 prompt 指令包，不是可执行代码——执行永远经由工具。

## 详细设计

- [skills_package](skills_package.md) — `SkillSpec` / `SkillPackage` / `SkillManifest` / `SkillResource` / `SkillScript` 数据合约
- [skills_activation](skills_activation.md) — `SkillActivation` / `SkillContext` / `SkillContribution` 激活合约
- [skills_runtime](skills_runtime.md) — `Skill` / `SkillRegistry` / `InMemorySkillRegistry` 与 bundled skill 的运行时（loop / schedule / skill_creator / agent_creator，default 项目另有 project_creator；agent_creator 引导用 `agent_define` 创建 agent，见 [agents](agents.md)；project_creator 引导用 `project_manage` 创建项目并接飞书机器人，见 [tools](tools.md)）
- [skills_package](skills_package.md) 的「实现现状」一节 — `MarkdownDocParser` / `FileSkillLoader` / `scan_skill_docs()` 文件 SKILL.md 加载

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- Skill 元数据（`SkillSpec` + `ComponentSpec`）。
- 激活合约：`activate(activation) → SkillContribution`（prompt 全文以 `prompt_blocks` 形式产出）。
- 注册发现：`SkillRegistry.register/get/discover`。
- 两阶段注入机制：每轮只注入「# 可用技能」名字+描述列表（`SkillsListingContributor`），模型按需调 `skill_load` 取全文——零常驻 token。
- 技能可见性（[project-design](../docs/design/project-design.md) §8）：agent 的 `AgentSpec.skills`（`None` = 全部，`()` = 无，名单 = 只这些，含 `*` = 全部；AGENT.md 不写时取项目 `agent_defaults.skills`）经 `config_snapshot["skills"]` → harness `_visible_skills()` → `TurnInputContext.visible_skills` / `ToolContext.visible_skills`：列表只列可见技能，`skill_load` 对不可见技能按不存在处理（`not_found`，列出的可用名单也只含可见技能）。

## Does Not Own

- 工具执行、LLM 调用、adapter 协议、持久记忆权威。
- AGENT.md 角色定义文档：与 SKILL.md 共用同一个解析器（`MarkdownDocParser` 按 frontmatter `kind` 区分），但目录分开（`~/.xana/skills/` vs 用户级 `~/.xana/agents/` + 项目级 `<project_root>/.xana/agents/`；项目级 skills 目录尚未加载）、消费方不同——skill 是做法库（经 `skill_load` 两阶段按需取全文），agent 是角色定义（可被 spawn 的 AgentSpec 来源），见 [agents](agents.md) 与 [agents_registry](agents_registry.md)。
- 资源解析、prompt 提供、上下文加载不设独立 ABC；文件加载只覆盖 SKILL.md/AGENT.md 这一种文档形态（见 [skills_package](skills_package.md) 实现现状）。

## Inputs

- `SkillActivation`（kind: manual/path/model/system + session/agent/run 上下文）
- bundled + 文件 skills registry（boot 组装的独立 `InMemorySkillRegistry`，文件 SKILL.md 与 bundled 注册进同一个）

## Outputs

- `SkillContribution.prompt_blocks` — 技能全文，经 `skill_load` 工具结果返回给模型
- 「# 可用技能」prompt 段（每轮系统提示，extension 注入）
