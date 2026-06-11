---
title: memory injector
type: doc
status: active
---

# memory injector

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_search_engine](memory_search_engine.md) — retrieval backend

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `InjectedBlock`
- [interfaces.py](../src/xana/memory/interfaces.py) — `MemoryInjector`, `MemoryInjectorRegistry`
- [markdown.py](../src/xana/memory/markdown.py) — `SearchMemoryInjector`

## 职责

`MemoryInjector` is L1 memory push into prompt assembly.

It owns:
- selecting memory blocks for a turn
- truncating output to its budget
- setting placement and reason metadata

It does not own:
- search scoring internals
- LLM calls
- tool execution
- active context persistence

## 接口语义

- `name()` returns a stable injector name.
- `select(session_id, agent_name, user_input, cwd)` returns prompt-ready `InjectedBlock` values.
- Registry composes injectors by priority and deduplicates node paths.

## 不变量

- Injector should be deterministic for same inputs.
- Injector should not call the LLM.
- Pull-style memory access belongs to tools, but should use the same `MemorySearchEngine` contract.
