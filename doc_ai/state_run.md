---
title: state run
type: doc
status: active
---

# state run

## 上级模块

- [state](state.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/state/contracts.py) — `RunRecord`、`RunStatus`、`ExitReason`、`Checkpoint`、`UsageSnapshot`、`SessionRecord`
- [interfaces.py](../src/xana/state/interfaces.py) — `RunStore`、`SessionStore`
- [file_runs.py](../src/xana/state/file_runs.py) — `FileRunStore`、`RunJsonlWriter`、`new_run_id`
- [in_memory.py](../src/xana/state/in_memory.py) — `InMemoryRunStore`

## 职责

`RunRecord` 是一次 agent 运行的状态机记录：status（running/paused/cancelled/failed/completed）、`exit_reason`（user_cancel / tool_kill_timeout / shutdown_graceful / shutdown_force 可恢复；network_error / llm_timeout / session_lost / tool_error / unknown 不可恢复）、`last_checkpoint`、`usage`、`input_request`（原始入站请求，/resume 靠它 fork）。`RunStore` 是记录边界，`Checkpoint` 是具名快照（turn_started / before_llm_request / … / turn_finalized）。

阶段二新增的 lineage 与护栏字段：

- `parent_run_id`：spawn 链的父 run（顶级 run 为 None），由 gateway spawn 路径经 metadata 写入。
- `label`：任务面板展示名（gateway inflight label 同款，用户输入前 40 字符）。
- `budget`：预算上限字典（如 `{"max_tokens": N}`），空 dict = 无上限；消费方是 [state_usage](state_usage.md) 的 BudgetTracker。

## FileRunStore（file_runs.py）

文件落盘的 RunStore 实现：

- 布局：`{root}/{run_id}.json`，一 run 一文件；全部写走 `_atomic_write`（tmp + `os.replace`），半写不产生半截记录。
- `create` 冲突抛 `FileExistsError`，不内部重试；`new_run_id()` 生成 8 字符 hex。
- `update_status(run_id, status, fields=None)`：读-改-写全量落盘，`fields` 合并进 patch（cancel 路径用它回写 `exit_reason`）。
- `checkpoint` / `update_usage`：整字段替换后落盘。
- `list_unfinished`：**只认 `exit_reason ∈ RESUMABLE_EXIT_REASONS` 的记录**；status 是 running/paused 但没有 exit_reason 的孤儿记录一律排除——它们应先由 [state_resume](state_resume.md) 的 startup_scan 盖上 `shutdown_force` 才进入候选。
- `list_all(*, status=None)`：**已在 `RunStore` ABC 上**（status 非 None 时按 `RunRecord.status` 过滤，如 `status="running"` 取在途集合）。消费者：/tasks 任务面板（见 [agent_adapter_commands](agent_adapter_commands.md)）+ startup_scan 孤儿清扫。
- `list_by_session(session_id)`：按会话过滤。

## RunJsonlWriter 与 .aborted.jsonl 语义

`RunJsonlWriter`（file_runs.py:146）写 per-run 消息流水 `{sessions_root}/runs/{run_id}.jsonl`（append `HistoryRow`，行格式与 history mirror 共用）：

- `finalize(run_id, status)`：追加 `{"marker": "run_completed", "status", "ts"}` 终止行——这是 startup_scan 判定 run 完成的依据（见 [state_resume](state_resume.md)）。
- `rename_to_aborted(run_id)`：把 `runs/{run_id}.jsonl` 改名为 `runs/{run_id}.aborted.jsonl`。**语义：该 run 已被 resume fork 消费过**——`/resume` 先把原 JSONL 改名（源留存备查，不再被 startup_scan 当未完成 run 重扫），再走普通 submit 路径用 `input_request["text"]` fork 新 run。文件不存在时静默 noop，重复 resume 不因此失败。

## InMemoryRunStore

同接口的内存实现（dict 承载），语义与 FileRunStore 一致（含同样的 `list_unfinished` 过滤和 ABC 上的 `list_all(status=...)`），供无 `history_root` 的 boot 路径使用（见 [runtime_boot](runtime_boot.md)）。

## SessionRecord 的 lineage 字段

`SessionRecord` 除身份字段外带 spawn 链 lineage：`parent_session_id` / `parent_agent` / `parent_run_id` / `depth`（顶级 session depth=0）。写点是 [agent_adapter_gateway](agent_adapter_gateway.md) 的 `GatewaySubagentDispatcher`（权威写点，持有完整 SubagentRequest）与 `GatewayAgentSpawner`（缺失时补齐可派生字段）。

## 不变量

- RunRecord 的状态迁移一律读-改-写落盘，不允许部分字段静默丢失。
- 判定"未完成"的权威是 `exit_reason`（resumable 集合），不是 status。
- `.aborted.jsonl` 后缀是"已 fork 消费"的墓碑，扫描器跳过它。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
