---
title: policy
type: doc
status: active
---

# policy

## 定位

权限、沙箱、approval 策略合约。仅接口合约，无实现——`src/xana/policy/` 只有 contracts.py / interfaces.py；approval 实际走 extension_api 的 `ApprovalReviewContributor`（见 [policy_approval](policy_approval.md)）。

## 详细设计

- [policy_permission](policy_permission.md) — Permission rules, contexts, requests, decisions.
- [policy_approval](policy_approval.md) — Ask-user approval provider seam.
- [policy_sandbox](policy_sandbox.md) — Sandbox policy planning and runner enter/exit.
- [policy_registry](policy_registry.md) — Registry for permission policy, approval provider, sandbox planner.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

权限、沙箱、approval 策略合约。
