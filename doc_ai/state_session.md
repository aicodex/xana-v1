---
title: state session
type: doc
status: active
---

# state session

## 上级模块

- [state](state.md) — 模块设计

## 职责

Session record and session store authority.

## 相关接口 / 类型

- `SessionRecord`
- `SessionStore`

## 代码锚点

- [contracts.py](../src/xana/state/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/state/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
