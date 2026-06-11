---
title: hooks runner
type: doc
status: active
---

# hooks runner

## 上级模块

- [hooks](hooks.md) — 模块设计

## 职责

Register, resolve, and run hook chains.

## 相关接口 / 类型

- `HookRegistry`
- `HookRunner`
- `HookResult`

## 代码锚点

- [contracts.py](../src/xana/hooks/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/hooks/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [hooks](./hooks.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
