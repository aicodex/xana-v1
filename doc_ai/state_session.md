---
title: state session
type: doc
status: active
---

# state session

## 上级模块

- [state](state.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/state/contracts.py) — `SessionRecord`
- [interfaces.py](../src/xana/state/interfaces.py) — `SessionStore`
- [in_memory.py](../src/xana/state/in_memory.py) — `InMemorySessionStore`

## 职责

`SessionRecord` 描述一个会话的静态身份与运行旋钮快照；`SessionStore` 是它的 CRUD 边界。当前**只有内存实现**（`InMemorySessionStore`，dict 承载），无文件实现——会话持久化目前由 [memory_history_mirror](memory_history_mirror.md)（jsonl 镜像）和 session_config（per-session JSON，见 [config](config.md)）分担，`state` 模块本身不感知这些。

## SessionRecord 字段语义

- `session_id` / `current_agent`：主键与当前 agent 名（必填）。
- `cwd` / `project_root`：会话绑定的工作目录与项目根。
- `model` / `effort`：会话级模型选择快照（与 session_config 的 `SessionConfig.model/effort` 对应，但不共享存储）。
- `active_skills`：已激活 skill 名列表。
- **`parent_session_id` / `parent_agent` / `parent_run_id` / `depth`**：subagent spawn 链 lineage 字段——记录本会话由哪个会话/哪个 run 派生、嵌套深度（顶级 session depth=0）。阶段二已有写点和消费方：写点是 [agent_adapter_gateway](agent_adapter_gateway.md) 的 `GatewaySubagentDispatcher`（权威写点）与 `GatewayAgentSpawner`（补齐），消费方是 dispatcher 的 `max_depth` 门禁（取父记录 depth + 1）。
- `metadata`：开放扩展袋。

## SessionStore 接口语义

- `get(session_id) -> SessionRecord | None`：不存在返回 `None`，不抛。
- `upsert(record)`：按 `session_id` 全量覆盖。
- `patch(session_id, fields) -> SessionRecord`：基于现有记录 `replace` 局部更新；key 不存在时 `InMemorySessionStore` 抛 `KeyError`。
- `list()` / `delete(session_id)`：列举 / 删除（删除幂等）。

## 不变量

- Session 记录是快照不是权威——运行期的权威上下文在 `ActiveContextStore`（见 [memory_active_context](memory_active_context.md)），持久镜像在 `HistoryMirror`。
- `parent_*`/`depth` 字段已有实现写点与消费方（spawn/dispatch 链路，见上），不再只是合约预留。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
