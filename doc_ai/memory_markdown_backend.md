---
title: memory markdown backend
type: doc
status: active
---

# memory markdown backend

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_tree](memory_tree.md) — tree contract
- [memory_search_engine](memory_search_engine.md) — search contract
- [memory_injector](memory_injector.md) — injection contract

## 代码锚点

- [markdown.py](../src/xana/memory/markdown.py) — concrete implementation
- [test_markdown_memory_implementation.py](../tests/test_markdown_memory_implementation.py) — behavior tests

## Concrete Classes

- `MarkdownMemoryTree(MemoryTree)`
- `SQLiteMemorySearchEngine(MemorySearchEngine)`
- `SearchMemoryInjector(MemoryInjector)`

## Markdown Layout

- `README_ai.md` is the root index.
- `doc_ai/module.md` is module design.
- `doc_ai/module_topic.md` is detailed design.
- `exp_ai/module_topic.md` is experience and migration memory.

## Link Semantics

- Markdown links are human-readable and machine-indexed.
- Frontmatter is minimal metadata, not a huge hidden reference list.
- `@path` / `@include path` are bounded include edges.
- SQLite is an index, not content authority.

## Install-State Rule

`README_ai.md`, `doc_ai/`, and `exp_ai/` must be copyable to `.xana/` and still build as a memory tree using only relative links.

Runtime memory edits should mutate the installed `.xana` copy, not the source repo docs.
