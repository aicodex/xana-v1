---
title: memory active context
type: doc
status: active
---

# memory active context

## 上级模块

- [memory](memory.md) — memory 模块设计

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `ActiveContext`, `MemoryMessage`, `Placeholder`（:73-86）
- [interfaces.py](../src/xana/memory/interfaces.py) — `ActiveContextStore`, `MemoryPolicy`（placeholder 三操作 :50-70）
- [active_context.py](../src/xana/memory/active_context.py) — `InMemoryActiveContextStore` 默认实现

## 职责

`ActiveContextStore` is the runtime authority for the context that a turn sees.

It owns:
- current session messages
- compacted summary
- pinned facts
- token estimate metadata

It does not own:
- durable storage authority
- search indexing
- adapter session protocol
- LLM request execution

## 接口语义

- `get(session_id, agent_name)` returns the current active context or `None`.
- `set(context)` replaces the active context authority for that session.
- `append_message(...)` appends one message and returns the updated context.
- `compact(...)` replaces selected messages with summary according to policy.
- `clear(...)` removes runtime context for the session.

## Placeholder 机制（contracts.py:73-86, interfaces.py:45-70）

`Placeholder` 是会话消息序列中的占位符：`run_id` + `label` + `status ∈ {running, completed, cancelled, failed}` + 可选 `jsonl_path`，标记「这里有一个 run，结果稍后回填」。它**与 `MemoryMessage` 平级**，由 `snapshot()` 返回为混合序列 `tuple[MemoryMessage | Placeholder, ...]`；**不**塞进 `ActiveContext.messages`（那里只放 `MemoryMessage`，保持类型纯净），回填时再从 snapshot 重算 messages 视图。

接口三操作（`InMemoryActiveContextStore` 已实现，且每个操作都镜像进 ContextSDK buffer）：

- `append_placeholder(session_id, agent_name, placeholder)` — 往 snapshot 尾部追加一个占位符。
- `replace_placeholder(run_id, replace_with)` — 按 `run_id` 在 snapshot 中找到该占位符，原位置替换为一条或多条 `MemoryMessage`；找不到返回 `None`。SDK 镜像路径里若被替换的是 `CompactPlaceholder`，会顺带释放 per-sid compact_lock（防锁泄漏，active_context.py:150-183）。
- `replace_range(session_id, agent_name, start_idx, end_idx, summary_message)` — 把 snapshot 的 `[start_idx, end_idx)` 区间折叠成单条 summary 消息（compact 的 legacy 写回路径）；下标越界返回 `None`。

用途：异步 run 先插占位符、完成后由写队列回填真实消息（见 [memory_history_mirror](memory_history_mirror.md)）；compact 把区间折叠为 summary。

## 不变量

- Normal turn execution reads active context, not history.
- History replay may rebuild active context only during recovery.
- A context update should be atomic from the caller perspective.
