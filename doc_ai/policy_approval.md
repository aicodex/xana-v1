---
title: policy approval
type: doc
status: active
---

# policy approval

## 上级模块

- [policy](policy.md) — 模块设计

## 职责

工具权限 non-allow 时的 approval 复核路径。

## 相关接口 / 类型

- `PermissionResult`（policy 合约）
- `ApprovalReviewContributor` / `ApprovalReviewInput`（extension_api）

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — `PermissionResult`
- [extension_api/interfaces.py](../src/xana/extension_api/interfaces.py) — `ApprovalReviewContributor`
- [default.py](../src/xana/harness_sdk/default.py) — 复核调用点

## 当前状态

policy 模块内没有 approval 实现。harness 在工具权限判定为 non-allow 时，调用 extension registry 中的 `ApprovalReviewContributor.review()`（`DefaultHarness` 工具分发内），第一个非 None 覆盖生效。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
