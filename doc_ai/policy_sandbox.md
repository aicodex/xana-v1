---
title: policy sandbox
type: doc
status: active
---

# policy sandbox

## 上级模块

- [policy](policy.md) — 模块设计

## 职责

沙箱策略的数据合约。

## 相关接口 / 类型

- `SandboxPolicySpec`

## 代码锚点

- [contracts.py](../src/xana/policy/contracts.py) — 数据结构和枚举

## 当前状态

只有 `SandboxPolicySpec` 数据合约，没有沙箱规划/执行实现。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
