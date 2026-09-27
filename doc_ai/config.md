---
title: config
type: doc
status: active
---

# config

## 定位

配置加载、系统/项目/agent 覆盖规则。

## 详细设计

- [config_sources](config_sources.md) — `ConfigSource`/`RawConfig`/`ConfigReader`；当前仅 system 层（`~/.xana/config/system.json`）有真实读取
- [config_merge](config_merge.md) — `RuntimeConfig` 组装；无通用合并器，实际解析在 `DefaultConfigProvider`
- [config_validation](config_validation.md) — 读取时 pydantic 校验；无合并后校验与热重载

## session 级覆盖的归属（doc_ai 缺口声明）

`src/xana/session_config/` 模块（`SessionConfig` / `SessionConfigStore` / `FileSessionConfigStore`，`~/.xana/sessions/{sid}.json`）承载 session 级覆盖，是 config 层级链的 session 层，但 **doc_ai 目前没有它的模块文档**（无 session_config.md / module 页）。在补齐之前，本文件即其归属声明；如需细节请直接读 `src/xana/session_config/` 三个文件。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

system.json 的读取、校验与 RuntimeConfig 组装；project/agent 层的合并规则（未实现）。

## Does Not Own

Runtime state mutation, agent behavior, adapter protocol, or model provider implementation.

## Precedence

设计层级：`system defaults -> project config -> agent config -> session override`（后两层未实现；session 层由 session_config 模块承担，见上）。

## Outputs

A typed config object, never raw scattered dictionaries.
