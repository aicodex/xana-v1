---
title: observability
type: doc
status: active
---

# observability

## 定位

health、trace、debug snapshot、诊断视图。仅接口合约，无实现——`src/xana/observability/` 只有 contracts.py / interfaces.py。

## 详细设计

- [observability_trace](observability_trace.md) — Trace event emission.
- [observability_metrics](observability_metrics.md) — Counters and gauges.
- [observability_health](observability_health.md) — Health report checks.
- [observability_inspection](observability_inspection.md) — Debug snapshots for operator inspection.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Health, trace events, debug snapshots, counters, and operator-facing inspection.

## Does Not Own

Runtime decisions. Observability may display state, not become a second state authority.
