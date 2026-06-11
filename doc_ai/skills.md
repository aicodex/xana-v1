---
title: skills
type: doc
status: active
---

# skills

## 定位

技能元数据、激活规则、上下文/提示注入。

## 详细设计

- [skills_package](skills_package.md) — Skill package, manifest, resources, scripts.
- [skills_activation](skills_activation.md) — Activation request, context loading, prompt contribution.
- [skills_runtime](skills_runtime.md) — Skill lifecycle: activate/deactivate and registry discovery.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Skill metadata, activation rules, context loading, prompt contribution, and optional fork/inline execution policy.

## Does Not Own

Tool execution, adapter protocol, LLM provider calls, or persistent memory authority.

## Inputs

- `SkillActivation`
- `SkillContext`

## Outputs

- prompt/context contribution
- activation events
