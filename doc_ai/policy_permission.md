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
- `PermissionRequest`
- `PermissionResult`

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — 数据结构和枚举

## 当前状态

只有数据合约，没有独立的权限策略类。每个工具的 `Tool.check_permission` 返回 `PermissionResult`，harness 直接消费；non-allow 时交给 `ApprovalReviewContributor` 复核（见 [policy_approval](policy_approval.md)）。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
