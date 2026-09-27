---
title: config sources
type: doc
status: active
---

# config sources

## 上级模块

- [config](config.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/config/contracts.py) — `ConfigSource`、`RawConfig`、`ConfigScope`
- [interfaces.py](../src/xana/config/interfaces.py) — `ConfigReader`
- [loader.py](../src/xana/config/loader.py) — `JsonSystemConfigReader`、`user_config_dir`、`default_system_json_source`、`ensure_user_system_json`、`seed_system_json`
- [system_model.py](../src/xana/config/system_model.py) — pydantic `SystemConfig` 内容 schema
- [defaults/system.json](../src/xana/config/defaults/system.json) — 种子文件
- [project_config.py](../src/xana/config/project_config.py) — 项目级 `config.json` 读取（`read_project_config`，`PROJECT_KEYS` 白名单）
- [paths.py](../src/xana/config/paths.py) — `xana_home` / `project_id` / `project_home` / `project_root`

## 职责

定义"配置从哪来、长什么样"。`ConfigSource` 标记一个来源（`scope: system/project/agent/session`、`path`、`priority`），`RawConfig` 是未类型化的 `{source, values}` 载体，`ConfigReader` 把来源读成 RawConfig。

## 现状：用户级 system.json + 项目级 config.json

- 用户级配置文件是 `~/.xana/config/system.json`（`$XANA_HOME` 环境变量可覆盖根目录，见 `user_config_dir()` → `xana_home()/config`）：模型、搜索服务、MCP、策略、全局工具禁用等。api key 等模型机密只放这里。
- 项目级配置文件是 `~/.xana/projects/<id>/config.json`（`project_config_path()`，`<id>` 来自 `project_id()`）：只能含 `PROJECT_KEYS`（channels / daemon / sessions / tools / agent_defaults），出现其他键 `read_project_config` 抛 `ProjectConfigError`；文件不存在视为空。机器人凭据在这里（chmod 600）。
- `default_system_json_source()` 返回它的 `ConfigSource(scope="system", priority=10)`。
- **文件不存在时 `ensure_user_system_json()` 用包内种子**（`xana/config/defaults/system.json`，内含空 models[]、default_model、limits、channels、web_search、mcp、policy 等默认结构）**播种**，并 chmod 600（文件会装 api key）。幂等，已存在则不动的。
- `JsonSystemConfigReader.read(source)` 读 JSON 并用 pydantic `SystemConfig(**raw)` 校验形状——shape 错误直接抛 `ValidationError`（校验即发生在读取层，见 [config_validation](config_validation.md)）。
- `ConfigScope` 里有 `project`/`agent`/`session`，但项目级 config.json 不经 `ConfigReader`，由 `DefaultConfigProvider` 直接调 `read_project_config` 叠加（见 [config_merge](config_merge.md)）；session 级覆盖不走本模块，落在 session_config 模块的 `FileSessionConfigStore`（归属声明见 [config](config.md)）。

## SystemConfig 内容面（system_model.py）

pydantic schema，顶层 `extra="forbid"`（roundtrip 可移植），主要段：`models[]`（ModelEntry：name/type/api_key/base_url/context_window/compact_override…）、`default_model`（chat/agent 各指一个模型名）、`model_tiers`、`limits`（per-provider 并发/rpm）、`channels`（telegram/feishu，项目级）、`web_search`、`mcp.servers`、`policy`（default_mode 白名单校验）、`tools.disabled`、`agent_defaults`（项目给 agent 的默认工具 / 技能选择）、`daemon`（项目级；unix_socket 缺省 `""` = `projects/<id>/run/serve.sock`，bind/port/token；bind 为空自动升级 127.0.0.1）、`sessions`（项目级，(adapter_id, thread_id)→sid 镜像，由 SessionResolver 维护，写回项目 config.json，见 [runtime_boot](runtime_boot.md)）。`SystemConfig` 表示用户级与项目级合并后的结果。`SystemConfig` 是内部实现 schema，不是合约——公开 frozen-dataclass 面是 `RawConfig`/`RuntimeConfig`。

## 不变量

- serve 进程内配置的读入口只有 `DefaultConfigProvider.load()`（system.json 经 `JsonSystemConfigReader`，再叠加项目 config.json）；SessionResolver 只在 boot 时经 provider 拿一次快照，自己不重读文件。不经 boot 的入口（xanac、setup、项目管理）用 `project_config.load_system_config(pid)` 同步读取同一合并结果。
- 种子播种保证 load 永远有文件可读；空 models[] 是合法状态（回落 fake provider）。

## 验收要求

- 输入/输出与 [config](./config.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
