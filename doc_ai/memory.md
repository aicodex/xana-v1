---
title: memory
type: doc
status: active
---

# memory

## 定位

`memory` owns runtime context authority, durable history mirror, persistent knowledge tree, and memory retrieval/injection contracts.

项目记忆(跨会话的长期记忆)在项目目录的 `memory/`:一条记忆一个文件(name / description / type / topic / pinned);每个会话强制注入索引与置顶记忆,每条消息由选择模型召回相关记忆的全文;模型侧写入口是 `memory_write`,每轮结束后另有后台提取兜底漏记。设计见 [memory-recall-design](../docs/design/memory-recall-design.md)、[memory-dream-design](../docs/design/memory-dream-design.md)。

## 详细设计

- [memory_active_context](memory_active_context.md) — `ActiveContextStore`，当前 turn 可见上下文的运行时权威
- [context](context.md) — `ContextSDK`，active context 的运行时载体与 compact-as-placeholder 协议
- [memory_history_mirror](memory_history_mirror.md) — `HistoryMirror` / `MemoryReplayer`，history 只是副本写和恢复输入
- [memory_tree](memory_tree.md) — `MemoryTree`，知识树节点和原子变更操作
- [memory_search_engine](memory_search_engine.md) — `MemorySearchEngine`，统一检索接口
- [memory_injector](memory_injector.md) — `ProjectMemoryInjector`:记忆索引 / 置顶 / 每轮召回
- [memory_markdown_backend](memory_markdown_backend.md) — markdown + SQLite concrete backend
- [memory_rules_gate](memory_rules_gate.md) — `memory_write`:项目记忆(`memory/`)唯一写入口、存储格式、旧记忆迁移
- [memory_extract](memory_extract.md) — 每轮结束后的后台记忆提取(所有运行,子 agent 除外)

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- Active context authority.
- Durable history mirror.
- Knowledge-tree storage contract.
- Search and injection contracts.

## Does Not Own

Agent behavior, LLM calls, adapter protocol, or tool execution.

## Rules

- Active context is the runtime authority.
- History is a message mirror plus necessary metadata.
- Normal runtime reads active context, not history.
- Cold start may replay history to rebuild active context.
- Markdown memory content is source of truth; SQLite/search indexes are rebuildable views.
- `todo_ai/` is a special memory tree for pending work, not design truth.
- 项目记忆(`memory/*.md`)是记忆的唯一权威,索引由它实时生成;模型侧唯一写入口是 `memory_write`(裸 Write/Edit 进 `memory/` 被 policy 拒绝),后台提取经 `ProjectMemoryStore` 只新建或追加。
- 记忆召回只看 `memory/`;项目里的其他 Markdown 只经 `memory_search` 检索。

## Inputs

- context updates
- message rows for mirror append
- replay request at boot
- memory node mutations
- search queries

## Outputs

- active context snapshot
- append acknowledgement
- replay result
- memory nodes
- search hits
- injected prompt blocks
