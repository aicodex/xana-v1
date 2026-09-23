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

## 职责

定义"配置从哪来、长什么样"。`ConfigSource` 标记一个来源（`scope: system/project/agent/session`、`path`、`priority`），`RawConfig` 是未类型化的 `{source, values}` 载体，`ConfigReader` 把来源读成 RawConfig。

## 现状：只有 system 层有真实读取

- 机器级配置文件是 `~/.xana/config/system.json`（`$XANA_HOME` 环境变量可覆盖根目录，见 `user_config_dir()`）。api key 等机密只放这里，不跟项目走。
- `default_system_json_source()` 返回它的 `ConfigSource(scope="system", priority=10)`。
- **文件不存在时 `ensure_user_system_json()` 用包内种子**（`xana/config/defaults/system.json`，内含空 models[]、channels、policy、daemon 等默认结构）**播种**，并 chmod 600（文件会装 api key）。幂等，已存在则不动的。
- `JsonSystemConfigReader.read(source)` 读 JSON 并用 pydantic `SystemConfig(**raw)` 校验形状——shape 错误直接抛 `ValidationError`（校验即发生在读取层，见 [config_validation](config_validation.md)）。
- `ConfigScope` 里有 `project`/`agent`/`session`，但**当前没有任何 project/agent/session 层的 ConfigReader 调用点**：session 级覆盖不走本模块，落在 session_config 模块的 `FileSessionConfigStore`（归属声明见 [config](config.md)）。

## SystemConfig 内容面（system_model.py）

pydantic schema，顶层 `extra="forbid"`（roundtrip 可移植），主要段：`models[]`（ModelEntry：name/type/api_key/base_url/context_window/compact_override…）、`default_model`（chat/agent 各指一个模型名）、`model_tiers`、`limits`（per-provider 并发/rpm）、`channels`（telegram/feishu）、`web_search`、`mcp.servers`、`policy`（default_mode 白名单校验）、`daemon`（unix_socket/bind/port/token；bind 为空自动升级 127.0.0.1）、`sessions`（(adapter_id, thread_id)→sid 镜像，由 SessionResolver 维护，见 [runtime_boot](runtime_boot.md)）。`SystemConfig` 是内部实现 schema，不是合约——公开 frozen-dataclass 面是 `RawConfig`/`RuntimeConfig`。

## 不变量

- system.json 的读入口只有 `DefaultConfigProvider.load()`（经 `JsonSystemConfigReader`）；SessionResolver 只在 boot 时经 provider 拿一次快照，自己不重读文件。
- 种子播种保证 load 永远有文件可读；空 models[] 是合法状态（回落 fake provider）。

## 验收要求

- 输入/输出与 [config](./config.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
