---
title: memory history mirror
type: doc
status: active
---

# memory history mirror

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_active_context](memory_active_context.md) — active context 权威

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `HistoryRow`, `ReplayResult`
- [interfaces.py](../src/xana/memory/interfaces.py) — `HistoryMirror`, `MemoryReplayer`

## 职责

`HistoryMirror` is a durable append mirror of context messages plus metadata.

It owns:
- append-only rows
- replay source material for cold start
- minimal metadata needed to rebuild active context

It does not own:
- live context authority
- current model-visible memory
- retry semantics for unfinished turns

## 接口语义

- `append(row)` writes a history row after active context is updated or as part of the same transaction boundary.
- `replay()` scans durable rows and reports loaded row/session counts.
- `MemoryReplayer.rebuild_active_context(...)` reconstructs `ActiveContextStore` from mirror rows.

## 不变量

- History is not the gold standard during normal runtime.
- History is a mirror and recovery input.
- If active context and history conflict during live runtime, active context wins.
