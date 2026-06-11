---
title: state
type: doc
status: active
---

# state

## 定位

session、run、checkpoint、resume、usage 状态合约。

## 详细设计

- [state_session](state_session.md) — Session record and session store authority.
- [state_run](state_run.md) — Run record, status, checkpoint, run store.
- [state_resume](state_resume.md) — Resume decisions for unfinished/paused runs.
- [state_usage](state_usage.md) — Usage snapshots and estimation.
- [state_repository](state_repository.md) — Repository facade for sessions and runs.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

session、run、checkpoint、resume、usage 状态合约。
