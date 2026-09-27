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
- [loader.py](../src/xana/config/loader.py) — `DefaultConfigProvider.load()`、`_resolve_default_model`、`derive_compact_thresholds`

## 职责

把（一个或多个）RawConfig 层收敛成一个类型化的 `RuntimeConfig`：`project_root`、`data_root`（`project_root/.xana/data`）、`default_model: ModelConfig`、`raw` 元组（保留来源溯源）。

## 现状：没有通用合并器，只有默认模型解析

- 没有通用合并器 ABC——project/agent 层的合并规则尚未落地。
- 实际承担"merge"角色的是 `DefaultConfigProvider.load()` 内部的 `_resolve_default_model(system)`：
  1. 读 `system.default_model.chat` 指定的模型名；
  2. 命中 `models[]` 里的 entry → 产出 `ModelConfig(provider=entry.type, model=entry.name, context_window, force_compact)`；
  3. 未配置或查无此 entry → 回落 `provider="fake", model="fake-model"`，保证全新机器也能 boot。
- `derive_compact_thresholds(entry)` 决定 compact 阈值：`entry.context_window` 缺省 967k；`force_compact` 取 `compact_override`（0 也有效），否则 `ctx - 16384(max_output) - 13000(headroom)`。
- 产出后 provider/model 被 boot 钉死为实际注册的 client（见 [runtime_boot](runtime_boot.md)），`RuntimeConfig` 不再中途变更。

## 层级与 session 覆盖的真实去向

config 模块文档面写的是 `system -> project -> agent -> session` 优先级，但当前代码里：

- **system 层**：本模块，已实现（上面）。
- **project / agent 层**：未实现。
- **session 层**：**不在本模块**——由 session_config 模块的 `FileSessionConfigStore`（`~/.xana/sessions/{sid}.json`）承载 `SessionConfig`（model/effort/streaming/thinking/search_provider/project_root 等运行旋钮），CLI 每轮前重读，无 mid-turn 变更。归属声明见 [config](config.md)。

## 不变量

- 出口永远是类型化 `RuntimeConfig`，不散落裸 dict（`raw` 只作溯源）。
- 默认模型解析失败回落 fake，绝不因配置缺失阻断 boot。
- 合并规则若将来落地 project/agent 层，须同步更新本文件。

## 验收要求

- 输入/输出与 [config](./config.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
