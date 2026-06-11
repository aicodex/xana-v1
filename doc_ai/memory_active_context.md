---
title: memory active context
type: doc
status: active
---

# memory active context

## 上级模块

- [memory](memory.md) — memory 模块设计

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `ActiveContext`, `MemoryMessage`
- [interfaces.py](../src/xana/memory/interfaces.py) — `ActiveContextStore`, `MemoryPolicy`

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

## 不变量

- Normal turn execution reads active context, not history.
- History replay may rebuild active context only during recovery.
- A context update should be atomic from the caller perspective.
