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

`MemorySearchEngine` 是项目目录 Markdown 的检索后端，供 `memory_search` 工具与 `memory_write` 写后增量索引使用。记忆的自动召回不走它（见 [memory_injector](memory_injector.md)：召回只看 `memory/`，由选择模型挑选，字面兜底在 `ProjectMemoryStore.lexical_rank`）。

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
- 持久文件库（boot 注入 `~/.xana/projects/<id>/sessions/search-{project_hash}.db`）connect 时按行水合，重启不丢索引；运行期靠 reindex 钩子增量更新（见 [memory_markdown_backend](memory_markdown_backend.md) 索引联动）。
- frontmatter `status: deprecated` 的节点从搜索命中中过滤（`_is_deprecated`，FTS 与 LIKE 两条路径都查）；现在只有 evolution 变更单的 supersede 链会产生这种节点（见 [evolution_module](evolution_module.md)）。
