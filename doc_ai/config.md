---
title: config
type: doc
status: active
---

# config

## 定位

配置加载与分层：用户级 `~/.xana/config/system.json` < 项目级 `~/.xana/projects/<id>/config.json`（[project-design](../docs/design/project-design.md) §4）；xana 路径的唯一解析点（`config/paths.py`）。

## 路径与项目配置

- [paths.py](../src/xana/config/paths.py)（只依赖标准库）：`xana_home()`（`$XANA_HOME`，缺省 `~/.xana`）；`project_id()`（`$XANA_PROJECT`，缺省 `DEFAULT_PROJECT="default"`，`xana serve --project <id>` 设置）；`project_home(id)` = `~/.xana/projects/<id>`（id 格式不合法抛 `ValueError`）；`project_root(id)` 读 `project_home/project.json` 的 `root`（未注册抛 `LookupError`）；`list_projects()` 列出有 `project.json` 的项目。
- [project_config.py](../src/xana/config/project_config.py)：`PROJECT_KEYS = (channels, daemon, sessions, tools, agent_defaults)`——项目 `config.json` 只能含这些键，出现其他键抛 `ProjectConfigError`（模型、凭据、搜索服务地址、策略不能由项目改）。`merge_system_config(user, project)` 按顶层键用项目值覆盖用户值后构造 `SystemConfig`；`load_system_config(pid)` 供不经 boot 的入口（xanac、setup、项目管理）同步读取合并结果；`update_project_config(path, **changes)` 原子改写若干项目键（chmod 600，值未变不写）；`write_project_sessions` 是 SessionResolver 的镜像写（只写 `sessions`）；`daemon_socket(daemon, pid)`：`daemon.unix_socket` 为空（默认）时取 `projects/<id>/run/serve.sock`。
- `SystemConfig.agent_defaults`（`AgentDefaults`：`tools` 缺省 `["default"]`、`disallowed_tools`、`skills` 缺省 `None` = 全部可见）是项目给 agent 的默认配置，见 [agents_spec](agents_spec.md)。

## 详细设计

- [config_sources](config_sources.md) — `ConfigSource`/`RawConfig`/`ConfigReader`；用户级 `system.json` 经 reader 读取，项目级 `config.json` 由 `project_config.py` 读取
- [config_merge](config_merge.md) — `RuntimeConfig` 组装；用户级 + 项目级按顶层键覆盖，实际解析在 `DefaultConfigProvider`
- [config_validation](config_validation.md) — 读取时 pydantic 校验；无合并后校验与热重载

## session 级覆盖的归属（doc_ai 缺口声明）

`src/xana/session_config/` 模块（`SessionConfig` / `SessionConfigStore` / `FileSessionConfigStore`，`~/.xana/projects/<id>/sessions/{sid}.json`）承载 session 级覆盖，是 config 层级链的 session 层，但 **doc_ai 目前没有它的模块文档**（无 session_config.md / module 页）。在补齐之前，本文件即其归属声明；如需细节请直接读 `src/xana/session_config/` 三个文件。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

xana 路径解析；system.json 与项目 config.json 的读取、校验、合并与 RuntimeConfig 组装；项目配置的写回（`update_project_config`）。agent 层配置由 AGENT.md 承担（见 [agents_spec](agents_spec.md)），不在本模块。

## Does Not Own

Runtime state mutation, agent behavior, adapter protocol, or model provider implementation.

## Precedence

`user system.json -> project config.json`（本模块，项目只能覆盖 `PROJECT_KEYS`）`-> agent（AGENT.md，未写 tools / skills 时取项目 agent_defaults）-> session override`（session_config 模块，见上）。

## Outputs

A typed config object, never raw scattered dictionaries.
