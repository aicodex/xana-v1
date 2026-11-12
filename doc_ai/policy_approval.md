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

harness 在工具判定非 allow 时先执行 extension 复核，再统一交给 PermissionService.authorize。必须询问的操作在执行副作用之前挂起；HTTP 回复和取消直接操作服务，不排队等待模型结束。

审批关联 session、根会话、run、call、参数及 cwd。单次批准或“本会话批准相同操作”只覆盖对应指纹；拒绝、取消、重复/过期回复不执行工具。子 agent 的审批同步到根会话。页面刷新通过快照恢复，服务重启不重放挂起操作。切换权限会唤醒并重新判定待执行操作。

测试：test_workbench_permissions.py；浏览器覆盖会话隔离、刷新与单次执行。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
