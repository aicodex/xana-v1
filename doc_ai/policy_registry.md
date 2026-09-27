---
title: policy registry
type: doc
status: active
---

# policy registry

## 上级模块

- [policy](policy.md) — 模块设计

## 职责

policy 组件注册表（当前不存在）。

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — 数据结构和枚举

## 当前状态

没有 policy 注册表：`src/xana/policy/` 只有数据合约，boot 也不注册任何 policy 组件。权限由各 `Tool.check_permission` 自行判定，approval 复核由 extension 的 `ApprovalReviewContributor` 提供。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
