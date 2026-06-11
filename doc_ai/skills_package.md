---
title: skills package
type: doc
status: active
---

# skills package

## 上级模块

- [skills](skills.md) — 模块设计

## 职责

Skill package, manifest, resources, scripts.

## 相关接口 / 类型

- `SkillManifest`
- `SkillPackage`
- `SkillResource`
- `SkillScript`

## 代码锚点

- [contracts.py](../src/xana/skills/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/skills/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [skills](./skills.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
