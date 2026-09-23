---
title: skills activation
type: doc
status: active
---

# skills activation

## 上级模块

- [skills](skills.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/skills/contracts.py) — `SkillActivation`, `SkillContext`, `SkillContribution`
- [skill_load.py](../src/xana/tools/builtin/skill_load.py) — 当前唯一的激活触发方
- [skills_listing.py](../src/xana/extension_api/builtin/skills_listing.py) — system kind 激活（列表注入）

## 职责

激活合约：谁在什么上下文下请求技能内容，技能返回什么。

## SkillActivation

- `kind ∈ {manual, path, model, system}`：触发来源。当前代码里实际用到两种——`skill_load` 工具构造 `kind="model"`（模型按需加载），`SkillsListingContributor` 构造 `kind="system"`（每轮列表枚举）。
- 其余字段：session_id、agent_name、run_id?、cwd?、args?、metadata。

`session_id`/`agent_name` 是必填，因为激活产物可能按会话缓存（`Skill.deactivate(session_id)` 以会话为粒度）。

## SkillContext / SkillContribution

- `SkillContext`：激活时注入的键值上下文（`values` dict）。当前 bundled 实现不读它。
- `SkillContribution`：激活的返回契约——`prompt_blocks`（prompt 全文块元组）、context、metadata。`skill_load` 把 `prompt_blocks` 用空行 join 后作为工具结果 `rendered` 返回给模型。

## 不变量

- 激活是**只读的、幂等的**：bundled 实现 `activate()` 不碰任何状态，`deactivate()` 是 no-op。
- 技能内容以 prompt 文本形式进模型上下文，没有 fork/inline 执行分支被实现（`SkillSpec.mode` 是声明位）。
- 失败路径：`skill_load` 查不到名字时返回 `failure_kind="not_found"`，并在 rendered 里列出可用技能名单。
