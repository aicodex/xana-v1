---
title: policy permission
type: doc
status: active
---

# policy permission

## 上级模块

- [policy](policy.md) — 模块设计

## 职责

Permission rules, contexts, requests, decisions.

## 相关接口 / 类型

- `PermissionRule`
- `PermissionContext`
- `PermissionPolicy`
- `PermissionRequest`
- `PermissionResult`

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/policy/interfaces.py) — 抽象接口

## 当前状态

仅接口合约，无实现。`src/xana/policy/` 下只有 contracts.py / interfaces.py，没有实现类。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
