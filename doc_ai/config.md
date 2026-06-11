---
title: config
type: doc
status: active
---

# config

## 定位

配置加载、系统/项目/agent 覆盖规则。

## 详细设计

- [config_sources](config_sources.md) — Read raw config from system/project/agent/session sources.
- [config_merge](config_merge.md) — Merge source layers into one typed RuntimeConfig using precedence rules.
- [config_validation](config_validation.md) — Validate merged config before runtime consumes it.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

System, project, and agent config loading and merge rules.

## Does Not Own

Runtime state mutation, agent behavior, adapter protocol, or model provider implementation.

## Precedence

`system defaults -> project config -> agent config -> session override`

## Outputs

A typed config object, never raw scattered dictionaries.
