---
title: policy approval
type: doc
status: active
---

# policy approval

## 上级模块

- [policy](policy.md) — 模块设计

## 职责

Ask-user approval provider seam.

## 相关接口 / 类型

- `ApprovalProvider`
- `ApprovalRequest`
- `ApprovalResult`

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/policy/interfaces.py) — 抽象接口

## 当前状态

仅接口合约，无实现。`src/xana/policy/` 下只有 contracts.py / interfaces.py，没有实现类。

注意：approval 的实际运行路径不走 `policy.ApprovalProvider`。harness 在工具权限判定为 non-allow 时，调用 extension registry 中的 `ApprovalReviewContributor.review()`（`src/xana/harness_sdk/default.py:303-311`），第一个非 None 覆盖生效；`ApprovalProvider` 当前既无实现也无调用方。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
