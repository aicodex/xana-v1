---
title: memory tree
type: doc
status: active
---

# memory tree

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_markdown_backend](memory_markdown_backend.md) — markdown concrete backend

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `MemoryNode`, `MemoryLintResult`
- [interfaces.py](../src/xana/memory/interfaces.py) — `MemoryTree`

## 职责

`MemoryTree` is the persistent knowledge-tree contract.

It owns:
- node addressing by path
- parent/child relationships
- lint findings
- atomic knowledge-tree mutations

It does not own:
- prompt injection packaging
- search scoring implementation
- markdown parsing details

## 节点语义

`MemoryNode.path` is the canonical address. In the Xana markdown layout this maps to relative markdown paths such as:

- `README_ai.md`
- `doc_ai/memory.md`
- `doc_ai/memory_tree.md`
- `exp_ai/memory_migration.md`

## 原子操作

- `split_node`
- `merge_nodes`
- `move_node`
- `redefine_parent`
- `delete_node`
- `rename_node`

Each mutation must succeed fully or leave the tree unchanged.

## 不变量

- Markdown body is the durable source of truth when using the markdown backend.
- Tree indexes are rebuildable from markdown links/frontmatter/includes.
- Direct memory writes should go through tree operations or a backend-specific equivalent that preserves tree validity.
