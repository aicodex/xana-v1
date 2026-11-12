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

`PermissionService` 由 boot 构造注入 harness。三档为 manual（始终询问）、yolo（必要时询问，缺省）、auto（全自动）。配置仅保存在对应 SessionConfig；None 表示未选择，不批量改写存量配置。子会话沿持久 parent_session_id 读取父级约束，不能提高权限。

飞书聊天例外：用户于 2026-11-09 明确要求默认全自动。收到飞书请求且该会话尚未选择权限时，request mapper 将 auto 保存到这一会话；已显式选择 manual/yolo 时尊重原选择。Web 和其他渠道缺省仍为 yolo，不修改项目、账户或定时任务配置。后台子 agent 仍沿既有父会话权限约束，不因来源变成独立的全自动路径。回归见 `test_feishu_session_defaults_to_auto_without_changing_other_sessions`。

Plan 仍以既有 metadata.mode 唯一持久标记，内部为 PermissionContext.plan_mode；它与三档模式独立。工具检查、extension 复核之后统一应用显式拒绝和 Plan 限制。manual 允许读取及 Kimi 对应的默认流程控制工具，其他需确认；yolo 自动常规操作，Tree-sitter Bash 分类危险命令后询问；auto 放行常规询问，但不绕过明确拒绝或 Plan，只禁用需要用户回答的 decision 通知。分类是审批启发式，不是 shell 安全沙箱。

## 验收要求

- 输入/输出与 [policy](./policy.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
