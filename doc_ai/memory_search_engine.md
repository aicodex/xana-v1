---
title: memory search engine
type: doc
status: active
---

# memory search engine

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_tree](memory_tree.md) — indexed node source

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `SearchQuery`, `SearchHit`
- [interfaces.py](../src/xana/memory/interfaces.py) — `MemorySearchEngine`
- [markdown.py](../src/xana/memory/markdown.py) — `SQLiteMemorySearchEngine`

## 职责

`MemorySearchEngine` is the unified retrieval backend used by both prompt injection and model-callable tools.

It owns:
- indexing nodes
- removing indexed nodes
- returning scored search hits

It does not own:
- active context authority
- markdown source of truth
- LLM reranking
- prompt placement

## 接口语义

- `index(node)` inserts or replaces one node in the index.
- `remove(node_path)` removes one node from the index.
- `search(query)` returns normalized higher-is-better `SearchHit` results.

## 不变量

- Search index is rebuildable.
- Search output carries `node_path` and may carry the `MemoryNode` for zero extra read.
- L1 injection and L2 tool query must share the same engine contract.
