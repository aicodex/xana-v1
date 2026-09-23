---
title: state resume
type: doc
status: active
---

# state resume

## 上级模块

- [state](state.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/state/contracts.py) — `ResumeDecision`、`ResumeAction`、`RESUMABLE_EXIT_REASONS`、`EXCLUDED_EXIT_REASONS`
- [interfaces.py](../src/xana/state/interfaces.py) — `ResumePolicy`（仅接口合约，无实现）、`RunStore.list_unfinished`
- [startup_scan.py](../src/xana/state/startup_scan.py) — `scan_runs_dir`、`replace_completed_placeholders`、`mark_unfinished_as_resumable`
- [gateway.py](../src/xana/agent_adapter/gateway.py) — `resume()` / `cancel()` / `cancel_all()`
- [commands.py](../src/xana/agent_adapter/commands.py) — `/resume` 命令

## 职责

定义"哪些 run 可恢复、如何恢复"。分两条路径：**运行中恢复**（/resume 命令）与**冷启动恢复**（startup_scan，boot 时仲裁上次进程留下的 run 文件）。

## 合约

- `RESUMABLE_EXIT_REASONS` = {user_cancel, tool_kill_timeout, shutdown_graceful, shutdown_force}；`EXCLUDED_EXIT_REASONS` = {network_error, llm_timeout, session_lost, tool_error, unknown}。`RunStore.list_unfinished` 只放行 resumable 集合（见 [state_run](state_run.md)）。
- `ResumePolicy.decide(run) -> ResumeDecision` 与 `ResumeAction`（resume_from_checkpoint / rerun_turn / drop / mark_cancelled）：**仅接口合约，当前无实现**。真实 resume 只有 fork 一种形态，不消费 checkpoint。
- `ResumeDecision` 数据类型保留在 contracts 里，供未来 policy 实现使用。

## /resume（运行中）

`DefaultCommandRouter._cmd_resume`：`/resume` 无参 → `run_store.list_unfinished()` 过滤当前 session，列出 `run_id · exit_reason · label`；`/resume <run_id>` → `gateway.resume(session_id, run_id)`。gateway 的 resume 语义（gateway.py:290）：

1. 防重入：同一 run_id 同时只能 resume 一次（`_resume_inflight`）。
2. `RunJsonlWriter.rename_to_aborted(run_id)` 把原 JSONL 改名 `.aborted.jsonl`（源留存，重扫跳过）。
3. 取 `rec.input_request["text"]` 作为 prompt，包装成新的 `InboundMessage` 走 `_submit` 普通管线 fork 新 run。

cancel 侧（与 resume 对称的记录语义）：`gateway.cancel` 给单个 run 标 `exit_reason=user_cancel`（并翻 live AbortSignal）；`cancel_all`（SIGTERM 入口）标 `shutdown_graceful`。

## startup_scan（冷启动，boot 时调用）

boot 有 `history_root` 时执行（见 [runtime_boot](runtime_boot.md)）：

- `scan_runs_dir(runs_root)` 产出 `ScanReport{completed_run_ids, unfinished_run_ids, aborted_run_ids}`。仲裁规则：**JSONL 里有 `marker == "run_completed"` 行则 completed 赢**，不管文件看起来是否完整；`.aborted.jsonl` 后缀的文件归入 aborted 并跳过（已是 resume fork 的源）。
- `replace_completed_placeholders(...)`：对每个 completed run，用 `runs_jsonl_to_memory_messages` 读出消息，`ActiveContextStore.replace_placeholder(run_id, messages)` 把占位符换成真实内容。
- `mark_unfinished_as_resumable(...)`：对每个 unfinished run 写/更新 RunRecord（`status=paused`、`exit_reason=shutdown_force`；已存在则 `update_status` 幂等更新）。随后**孤儿清扫**：凡 `status ∈ {running, paused}` 且无 `exit_reason` 的记录（pre-jsonl 崩溃或日志丢失）也盖上 `shutdown_force`，让它们能被 `list_unfinished` 浮出。

## 不变量

- "未完成"的判定权威是 exit_reason 集合，不是文件长短也不是 status。
- startup_scan 幂等：重复 boot 不产生重复记录。
- completed run 的 PH 替换与 unfinished 的 resumable 标记互不重叠（由 marker 仲裁二分）。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
