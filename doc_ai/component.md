---
title: component
type: doc
status: active
---

# component

## 定位

共享组件元数据、加载、校验、override、registry 合约。仅接口合约，无实现——`src/xana/component/` 只有 contracts.py / interfaces.py。

## 详细设计

- [component_spec](component_spec.md) — Shared component metadata and source identity.
- [component_loading](component_loading.md) — Load roots and report loaded/disabled/errors/source maps.
- [component_override](component_override.md) — Source priority, override resolution, override logs.
- [component_registry](component_registry.md) — Register/get/list/remove components.
- [component_cache](component_cache.md) — Load cache key/entry/invalidation.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

共享组件元数据、加载、校验、override、registry 合约。
