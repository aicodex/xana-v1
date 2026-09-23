---
title: memory
type: doc
status: active
---

# memory

## 定位

`memory` owns runtime context authority, durable history mirror, persistent knowledge tree, and memory retrieval/injection contracts.

## 详细设计

- [memory_active_context](memory_active_context.md) — `ActiveContextStore`，当前 turn 可见上下文的运行时权威
- [context](context.md) — `ContextSDK`，active context 的运行时载体与 compact-as-placeholder 协议
- [memory_history_mirror](memory_history_mirror.md) — `HistoryMirror` / `MemoryReplayer`，history 只是副本写和恢复输入
- [memory_tree](memory_tree.md) — `MemoryTree`，知识树节点和原子变更操作
- [memory_search_engine](memory_search_engine.md) — `MemorySearchEngine`，统一检索接口
- [memory_injector](memory_injector.md) — `MemoryInjector` / registry，L1 prompt 注入
- [memory_markdown_backend](memory_markdown_backend.md) — markdown + SQLite concrete backend
- [memory_rules_gate](memory_rules_gate.md) — `memory_write`，L2 规则唯一写入口（查重门禁 + supersede 链）

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
- L2 规则条目唯一写入口是 `memory_write` 工具（查重门禁 + supersede 链）；裸 Write/Edit 进 `L2/` 被 policy 拒绝。

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
