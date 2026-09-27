---
title: policy
type: doc
status: active
---

# policy

## 定位

权限、沙箱、approval 的数据合约。`src/xana/policy/` 只有 contracts.py（interfaces.py 为空，不定义 ABC）；权限判定走 `Tool.check_permission`，approval 实际走 extension_api 的 `ApprovalReviewContributor`（见 [policy_approval](policy_approval.md)）。

## 详细设计

- [policy_permission](policy_permission.md) — Permission rules, contexts, requests, decisions.
- [policy_approval](policy_approval.md) — approval 的实际运行路径（ApprovalReviewContributor）。
- [policy_sandbox](policy_sandbox.md) — `SandboxPolicySpec` 数据合约。
- [policy_registry](policy_registry.md) — 当前无 policy 注册表。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

权限、沙箱、approval 策略合约。
