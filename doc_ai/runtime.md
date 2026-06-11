---
title: runtime
type: doc
status: active
---

# runtime

## 定位

进程生命周期、启动顺序、调度外壳。

## 详细设计

- [runtime_boot](runtime_boot.md) — Boot order: load config, restore memory, build components, start adapters.
- [runtime_lifecycle](runtime_lifecycle.md) — Component install/start/stop/uninstall lifecycle.
- [runtime_scheduler](runtime_scheduler.md) — Spawn/cancel/status task shell without owning task behavior.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Process lifecycle, boot order, shutdown, scheduler shell, and top-level dependency wiring.

## Does Not Own

Agent behavior, tool execution logic, LLM provider logic, memory semantics, or adapter protocol details.

## Inputs

- `RuntimeConfig`
- component registries
- adapter factories
- memory restore result

## Outputs

- runtime health
- lifecycle events
- started/stopped component handles

## Lifecycle

`load_config -> restore_memory -> build_registries -> start_harness -> start_adapters -> accept_inbound -> shutdown`
