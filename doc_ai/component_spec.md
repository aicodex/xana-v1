---
title: component spec
type: doc
status: active
---

# component spec

## 上级模块

- [component](component.md) — 模块设计

## 职责

Shared component metadata and source identity.

## 相关接口 / 类型

- `ComponentSpec`
- `ComponentSource`

## 代码锚点

- [contracts.py](../src/xana/component/contracts.py) — 数据结构和枚举

## 当前状态

只有数据合约。`src/xana/component/` 下只有 contracts.py，没有 ABC、实现类和 boot 注册；`ComponentSpec` 作为工具/skill/agent spec 的 `component` 字段被直接引用。

## 验收要求

- 输入/输出与 [component](./component.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
