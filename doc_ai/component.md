---
title: component
type: doc
status: active
---

# component

## 定位

共享组件元数据合约。`src/xana/component/` 只有 contracts.py，不定义 ABC、无实现；`ComponentSpec` / `ComponentSource` 被工具、skill、agent 声明直接引用。

## 详细设计

- [component_spec](component_spec.md) — Shared component metadata and source identity.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

共享组件元数据合约。
