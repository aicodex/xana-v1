---
title: config merge
type: doc
status: active
---

# config merge

## 上级模块

- [config](config.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/config/contracts.py) — `RuntimeConfig`、`ModelConfig`、`RawConfig`
- [loader.py](../src/xana/config/loader.py) — `DefaultConfigProvider.load()`、`_resolve_default_model`、`resolve_model_config`
- [project_config.py](../src/xana/config/project_config.py) — `merge_system_config`、`read_project_config`、`PROJECT_KEYS`

## 职责

把（一个或多个）RawConfig 层收敛成一个类型化的 `RuntimeConfig`：`project_root`、`data_root`（`project_root/.xana/data`）、`default_model: ModelConfig`、`raw` 元组（保留来源溯源）。

## 现状：用户级 + 项目级按顶层键覆盖，再解析默认模型

- 没有通用合并器 ABC。`DefaultConfigProvider(project_config=None)` 的 `project_config_path` 缺省为 `projects/<当前项目>/config.json`；`load()` 读用户级 system.json 后调 `merge_system_config(user, read_project_config(...))`：项目配置按**顶层键整体覆盖**用户配置（`{**user, **project}`，不做深合并），只允许 `PROJECT_KEYS`（channels / daemon / sessions / tools / agent_defaults）。例外：`tools.disabled` 取并集——用户级禁用对所有项目生效，项目只能再多禁、不能放开。合并结果缓存为 `SystemConfig`（`load_system_config()` 取）。
- 实际承担"merge"角色的是 `DefaultConfigProvider.load()` 内部的 `_resolve_default_model(system)`：
  1. 读 `system.default_model.chat` 指定的模型名；
  2. 命中 `models[]` 里的 entry → 产出 `ModelConfig(provider=entry.type, model=entry.name, context_window, force_compact)`；
  3. 未配置或查无此 entry → 明确报错，禁止隐式选择 fake。显式选择 `models[].type="fake"` 的条目才能运行模拟模型。
- `resolve_model_config(entry)` 从 `context_window` / `max_tokens` 统一推导预算，`compact_override` 可覆盖自动线；配置不完整时自动线只减 64Ki。完整公式、摘要请求参数和显示语义见 [harness_compact](harness_compact.md#阈值与失败)。
- 产出后 provider/model 被 boot 钉死为实际注册的 client（见 [runtime_boot](runtime_boot.md)），`RuntimeConfig` 不再中途变更。

## 层级与 session 覆盖的真实去向

优先级 `user system.json -> project config.json -> agent -> session`，当前代码里：

- **用户级 / 项目级**：本模块，已实现（上面）。
- **agent 层**：不在本模块——AGENT.md 的 tools / skills 未写时取项目 `agent_defaults`（见 [agents_spec](agents_spec.md)）。
- **session 层**：**不在本模块**——由 session_config 模块的 `FileSessionConfigStore`（`~/.xana/projects/<id>/sessions/{sid}.json`）承载 `SessionConfig`（model/effort/streaming/thinking/search_provider/view_mode/agent 等运行旋钮），CLI 每轮前重读，无 mid-turn 变更。归属声明见 [config](config.md)。

## 不变量

- 出口永远是类型化 `RuntimeConfig`，不散落裸 dict（`raw` 只作溯源）。
- 默认模型解析失败时阻止 boot；setup 可写入尚未完成的配置，但 serve 必须配置完成才能启动。
- 项目配置出现 `PROJECT_KEYS` 以外的键即报错，不静默忽略。

## 验收要求

- 输入/输出与 [config](./config.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。

## 显式模拟模型

仅测试或离线演示需要模拟回复时，在 system.json 显式配置：

```json
{"models": [{"name": "offline", "type": "fake"}], "default_model": {"chat": "offline"}}
```

harness 直接调用方也必须显式提供 `config_snapshot.provider`；注册 fake 客户端并不代表选用它。
新安装的空配置不自动补 fake；已有配置不自动改写，缺失的真实凭据须由使用者补齐。
验证：`tests/test_explicit_fake.py`、`tests/test_provider_resolution.py`。
