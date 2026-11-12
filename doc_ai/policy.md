---
title: policy
type: doc
status: active
---

# policy

## 定位

会话权限和审批的统一执行边界。`contracts.py` 定义契约，`approvals.py::PermissionService` 维护模式、审批和会话授权，`shell.py` 解析危险命令。工具检查与 extension 复核后仍须经过最终会话约束；不新增 ABC。

## 详细设计

- [policy_permission](policy_permission.md) — Permission rules, contexts, requests, decisions.
- [policy_approval](policy_approval.md) — 待审批操作、取消与授权范围。
- [policy_sandbox](policy_sandbox.md) — `SandboxPolicySpec` 数据合约。
- [policy_registry](policy_registry.md) — 当前无 policy 注册表。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

权限、沙箱、approval 策略合约。
