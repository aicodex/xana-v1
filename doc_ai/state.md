---
title: state
type: doc
status: active
---

# state

## 定位

session、run、checkpoint、resume、usage 状态合约。

## 详细设计

- [state_session](state_session.md) — `SessionRecord` / `SessionStore`；仅内存实现，`parent_*` 为 subagent 预留字段
- [state_run](state_run.md) — `RunRecord` / `RunStore` / `Checkpoint`；`FileRunStore` 落盘、`RunJsonlWriter` 与 `.aborted.jsonl` 墓碑语义
- [state_resume](state_resume.md) — resumable exit_reason 集合、/resume fork 语义、boot 时 startup_scan 冷启动仲裁
- [state_usage](state_usage.md) — `UsageSnapshot` 估算/精确双轨字段；`BudgetTracker` 预算护栏
- [state_repository](state_repository.md) — `StateRepository` 门面；`_CompositeStateRepository`（run 落盘 + session 内存）

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

session、run、checkpoint、resume、usage 状态合约。
