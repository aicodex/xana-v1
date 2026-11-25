---
title: policy
type: doc
status: active
---

# policy

## 定位

独立执行环境与会话权限边界（零对 `harness_sdk`/`session_config`/`state`/`tools` 依赖，仅依赖 `xana.protocol` 与 `tree_sitter`）。

- `contracts.py`：冻结数据契约（受 `tests/golden/policy_contract_v1.json` 锁定），含 `AuthorizationRequest`、`RiskAssessment`、`ProcessSpec`、`ProcessOutcome`、`SessionPolicySnapshot` 等。
- `interfaces.py`：5 个 ABC — `CommandRiskClassifier`、`PathBoundaryPolicy`、`ExecutionEnvironment`、`PolicyStatePort`、`PolicyEventSink`（另含结构化 `AbortLike` Protocol）。
- `approvals.py`：`PermissionService` 维护三档模式（manual/yolo/auto）、plan mode、子会话权限上限与待审批流；宿主通过 `runtime/policy_adapter.py` 注入持久与事件端口。
- `shell.py`：`TreeSitterShellClassifier`（实现 `CommandRiskClassifier`，可进化策略槽位）。
- `path_policy.py`：`ProtectedDirsPathPolicy`（统一 `Write`/`Edit` 的受保护目录门禁与可选工作区写入边界）。
- `exec_env.py`：`LocalProcessEnvironment`（POSIX 进程组隔离、超时/取消孤儿清理、可选 `RLIMIT_AS`）；`testing.py` 提供 `FakeExecutionEnvironment` / `InMemoryPolicyState` / `RecordingPolicyEventSink`。
- `tape_recorder.py`：`PolicyTapeRecorder` 直写 Stage 0 `TapeWriter`（`policy.decision` / `env.exec`）；内置可开关诊断日志 `DiagLogger("policy")` / `DiagLogger("env")`。
- `__main__.py`：独立 CLI `python -m xana.policy <bench|check>`。

## 详细设计

- [policy_permission](policy_permission.md) — Permission rules, contexts, requests, decisions.
- [policy_approval](policy_approval.md) — 待审批操作、取消与授权范围。
- [policy_sandbox](policy_sandbox.md) — `SandboxPolicySpec` 数据合约。
- [policy_registry](policy_registry.md) — 当前无 policy 注册表。

## 全局合约

- [README_ai](README_ai.md) — L0 + 7 层架构总览
- [modular_interfaces](modular_interfaces.md#3-stage-2policy--执行环境srcxanapolicy) — L2 全部 ABC 签名与契约速查
- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

权限、沙箱、approval 策略合约。
