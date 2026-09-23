---
title: tools permission sandbox
type: doc
status: active
---

# tools permission sandbox

## 上级模块

- [tools](tools.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/tools/interfaces.py) — `ToolPermissionPolicy` / `ToolSandboxPolicy` / `ToolContextBuilder`（仅合约）
- [default.py](../src/xana/harness_sdk/default.py) — `check_permission` 编排与 `approval_review` 覆写
- [policy/contracts.py](../src/xana/policy/contracts.py) — `PermissionContext`, `PermissionResult`, `SandboxPolicySpec`

## 职责

工具执行前的权限与沙箱接缝。分两层：全局权限策略在 `policy` 模块；本层是**工具自带**的 `check_permission` 闸门，由 harness 在 `validate` 之后、`execute` 之前调用。

## 工具级 check_permission（已实现）

- 大多数内置工具恒返回 `PermissionResult(decision="allow")`，把门禁留给上层（如 Bash 的注释："gating delegated to caller"）。
- 例外是文件写类工具的**受保护目录门禁**：
  - `WriteTool` / `EditTool` 构造可接收 `protected_dir`（`Path | tuple[Path, ...] | list[Path]`，支持多目录）；目标路径解析后落在任一受保护目录内 → `decision="deny"`，理由是规则目录的唯一写入口是 `memory_write` 工具、Agent 文档的唯一变更路径是 episode 提案→评估→判决闭环（见 [evolution_module](evolution_module.md)）。
  - boot 已接线（boot.py:234-237）：`protected_dir=(project_root / "L2", ~/.xana/agents)` 双目录注入两工具，同时注入 `memory_tree` 启用写后 reindex。门禁只罩 `L2/` 规则目录与 Agent 文档目录，树内其他 markdown 文件照常可写（写后走 `_reindex` 增量更新索引）。
- `web_fetch` / `web_search` 的 `validate` 阶段也做入口校验（URL 必须是 http/https）。

## 编排层覆写（approval_review）

`check_permission` 返回非 allow 时，harness 依次调用 extension registry 的 `approval_review` contributors（`ApprovalReviewInput`），第一个返回非 None 的 `PermissionResult` 覆写原决定。这允许 UI/审批扩展把 deny 升级成 allow（或反向），而不动工具实现。

## 沙箱

- `ToolSpec.sandbox_policy`（`SandboxPolicySpec`：mode / writable_roots / readable_roots / allow_network / allow_subprocess / env）是声明位；harness 分发循环目前不强制，写越界防护由具体工具自行实现（如 `TodoStore._path` 拒绝含 `/` 的 session_id，`EnterWorktree` 校验 name 与 session_id）。
- `ToolSandboxPolicy` / `ToolContextBuilder` / `ToolPermissionPolicy` 三个 ABC **仅接口合约，无实现**：harness 直接用 `Tool.check_permission`，没有独立的 policy 策略类。

## denial 结果

非 allow 且无覆写时，harness 走 `_denied_result`：固定 `failure_kind="policy_forbidden"`（reason 透传），不执行工具。`ToolFailureKind` 里的 `approval_denied` / `approval_aborted` 是合约预留值，当前编排层未区分产生。
