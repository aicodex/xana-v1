---
title: observability inspection
type: doc
status: active
---

# observability inspection

## 上级模块

- [observability](observability.md) — 模块设计

## 职责

Debug snapshots for operator inspection.

## 相关接口 / 类型

- `Inspector`
- `DebugSnapshot`

## 代码锚点

- [contracts.py](../src/xana/observability/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/observability/interfaces.py) — 抽象接口

## 当前状态

仅接口合约，无实现。`src/xana/observability/` 下只有 contracts.py / interfaces.py，没有实现类和 boot 注册。

## 验收要求

- 输入/输出与 [observability](./observability.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
