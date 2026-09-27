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

## L1 索引联动（阶段一落地）

markdown 是 source of truth；SQLite 索引靠三条链路保持一致，重启后无需全量重建：

1. **持久索引文件**：boot 把引擎建在当前项目的 `~/.xana/projects/<id>/sessions/search-{project_hash}.db`（`project_hash` = project_root 路径 sha256 前 16 位，boot.py `_search_db_path`；路径经 `config/paths.project_home()`）。文件库让索引跨重启保留：connect 时 `_hydrate_from_disk()` 把 `memory_nodes` 表里的行水合回内存 `nodes`，重启即可搜索。
2. **boot 幂等校准**：`boot()` 里 `tree.load()` 后恒跑 `engine.rebuild_from_tree(tree)`（boot.py:165-168）——markdown 侧为准全量重写索引表，把运行期漂移校准回来。
3. **运行期增量 reindex**：
   - `MarkdownMemoryTree.reindex_paths(paths)`（markdown.py:281-317）——运行期 markdown 编辑后的增量刷新。逐路径：root 外的或非 `.md` 的直接忽略；文件已删 → 从 node map 摘除并 `engine.remove()`；文件还在 → 重载该文件节点并重挂 children，再 `engine.index(node)`。返回实际刷新的相对路径。
   - 文件写工具的 post_tool_use 钩子 `_reindex.maybe_reindex_markdown(tree, file_path, cwd)`（tools/builtin/_reindex.py）——Write/Edit 成功后，若目标是以 tree.root 为根的 `.md` 文件则触发 `reindex_paths` 单文件刷新，`memory_search` 立即看到新内容，无需重启。L2 规则写工具 `memory_write` 落盘后也直接调 `tree.reindex_paths()`（见 [memory_rules_gate](memory_rules_gate.md)）。

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
