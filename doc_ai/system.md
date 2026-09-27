---
title: System Design
type: doc
status: active
---

# System Design

## 相关入口

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [runtime](runtime.md) — 启动和生命周期
- [harness_sdk](harness_sdk.md) — turn lifecycle
- [memory](memory.md) — active context 和 history mirror
- [agent_adapter](agent_adapter.md) — 外部协议入口

## Core Idea

The system is organized around explicit contracts. Each module has:
- input types
- output types
- lifecycle hooks
- ownership boundaries
- forbidden dependencies

## State Authorities

- Active runtime context: `memory.ActiveContextStore`.
- Durable conversation mirror: `memory.HistoryMirror`.
- Turn lifecycle: `state.RunStore`（`RunRecord.status`），由 `DefaultHarness` 驱动转换。
- Model choice and effort: `config` plus session state, not adapter state.
- External protocol state: `agent_adapter`, only for transport concerns.

## Normal Message Flow

`Adapter -> Agent outer API -> Harness -> LLM Driver -> Tool/Skill dispatch -> Harness events -> Agent -> Adapter render`

## Recovery Flow

`Config -> HistoryMirror replay -> ActiveContextStore -> startup_scan（run 仲裁）-> Adapters start`

History is only a mirror unless the memory contract is explicitly changed.
