---
title: hooks
type: doc
status: active
---

# hooks

## 定位

生命周期 hook 合约。仅接口合约，无实现——`src/xana/hooks/` 只有 contracts.py / interfaces.py；`HarnessContext.hook_runner` 字段存在但无人消费，无 `HookRunner` 实现（见 [hooks_runner](hooks_runner.md)）。

## 详细设计

- [hooks_spec](hooks_spec.md) — Hook metadata, matchers, handler refs.
- [hooks_turn_model](hooks_turn_model.md) — Pre/post turn and pre/post model request hooks.
- [hooks_tool](hooks_tool.md) — Pre/post tool use hooks.
- [hooks_runner](hooks_runner.md) — Register, resolve, and run hook chains.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

生命周期 hook 合约。
