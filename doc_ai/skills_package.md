---
title: skills package
type: doc
status: active
---

# skills package

## 上级模块

- [skills](skills.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/skills/contracts.py) — `SkillSpec`, `SkillPackage`, `SkillManifest`, `SkillResource`, `SkillScript`
- [markdown_loader.py](../src/xana/skills/markdown_loader.py) — `MarkdownDocParser`, `FileSkillLoader`, `MarkdownSkill`, `scan_skill_docs()`

## 职责

Skill 的数据合约层：一个技能是一个「manifest + 指令路径 + 资源 + 脚本」的包声明。

## 类型

- `SkillManifest`：name、description、version?、when_to_use、allowed_tools、activation_paths、metadata。
- `SkillPackage`：root?、manifest、instructions_path?、resources（`SkillResource`：kind ∈ instructions/reference/template/asset/script）、scripts（`SkillScript`：name/path/args_schema/allowed_tools）。
- `SkillSpec`：对外完整声明 = `ComponentSpec`（kind="skill"）+ when_to_use + package + mode（inline/fork）+ allowed_tools + trust_level + arg_schema + model/effort 覆盖位 + user_invocable / disable_model_invocation 开关。

## 实现现状（markdown_loader.py）

`SkillManifestParser` / `SkillLoader` 已各有首个实现（计划 §5.1），位于 [markdown_loader.py](../src/xana/skills/markdown_loader.py)：

- `MarkdownDocParser`（`SkillManifestParser` 首个实现）：解析 frontmatter（YAML）+ 正文的 Markdown 文档。**同一解析器按 frontmatter 的 `kind` 字段区分 skill（默认）与 agent**（未知 kind 抛错），agent kind 的产出由 [agents/registry.py](../src/xana/agents/registry.py) 消费。
- 校验规则（失败抛 `SkillManifestError`，带 path/kind/field/reason 四个类型化字段）：
  - kind=skill：必填非空 `name` / `description`；可选 `when_to_use`（字符串）、`allowed_tools`（字符串列表）。
  - kind=agent：必填非空 `name` / `goal`（goal 映射到 `manifest.description`）、`tools`（非空字符串列表 → `allowed_tools`）；可选 `skills` / `budget`（mapping）/ `stop_rules` / `output_contract` 放入 `metadata`，由 AgentRegistry 消费。
  - frontmatter 本身：必须有成对的 `---` 围栏、必须是 YAML mapping。
- `split_frontmatter(text, *, path)`：切出 (frontmatter dict, 正文)；无 frontmatter 返回 `({}, text)`。
- `FileSkillLoader`（`SkillLoader` 首个实现）：source 是 SKILL.md 路径或其所在目录；产出 `MarkdownSkill`。若文档 `kind=agent` 则抛 `SkillManifestError`（AGENT.md 是角色定义，走 AgentRegistry，不是 skill）。
- `MarkdownSkill`：文件 SKILL.md 的 `Skill` 实现，`activate()` 返回正文作为 `prompt_blocks`——与 bundled skill 同一个接口、同一个两阶段加载路径。
- `scan_skill_docs(root, *, loader)`：扫描 `{root}/*/SKILL.md`，返回 `(skills, errors)`；**坏文档收集为错误返回，不中断其余文档**；root 不是目录时返回空。
- boot 从 `{XANA_HOME}/skills`（默认 `~/.xana/skills`，`XANA_HOME` 环境变量可覆盖）扫描，坏文档记 warning 跳过（见 [runtime_boot](runtime_boot.md)）。

仍未实现的：bundled skill 的「包」字段仍是占位（`root=None, instructions_path=None`，全文为内嵌 `_BODY`）；`SkillResource` / `SkillScript` 只活在合约里；`SkillSpec` 的 mode/fork、model/effort 覆盖、activation_paths 匹配等仍是声明位，无消费方。`SkillResourceResolver` / `SkillPromptProvider` / `SkillContextLoader` 三个 ABC 仍无实现。
