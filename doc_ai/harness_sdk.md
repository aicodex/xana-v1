---
title: harness_sdk
type: doc
status: active
---

# harness_sdk

## 定位

turn loop、prompt 组装、工具调度、auto-compact、暂停/取消、事件流和 usage 快照。唯一实现是 `DefaultHarness`。

## 详细设计

- [harness_core](harness_core.md) — `DefaultHarness` turn loop、sync/async 双路径、PH ritual、fork store、事件流合约。
- [harness_prompt_assembly](harness_prompt_assembly.md) — 会话内固定的 system prompt（7 静态段 + 拼装）、ContextNote（记忆召回/技能/按需工具/todo 提醒）、compact prompt。
- [harness_tool_dispatch](harness_tool_dispatch.md) — 工具调用解析与 validate→permission→execute 管线、deferred 工具、取消宽限期。
- [harness_compact](harness_compact.md) — auto-compact 同步触发、9 节摘要、Compact-as-PH、revealed tools 保留。
- [harness_usage](harness_usage.md) — `UsageSnapshot` 累积规则（exact vs estimated、cache token 并回）。
- [harness_turn_controller](harness_turn_controller.md) — turn 状态转换（内联在 `DefaultHarness`）、`AbortSignal` 取消机制与 Stop 决策。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- 单 turn 内的 model↔tool 往返循环（工具轮上限读 `AgentSpec.max_turns`，缺省 25）；纯文本结束时先走 Stop 决策（见 [harness_turn_controller](harness_turn_controller.md)）
- system prompt 会话内固定（按会话缓存）；tools_schema 每轮组装（deferred 揭示后下一轮生效）；每轮变化的上下文以 ContextNote 追加进历史
- auto-compact 的同步触发与摘要生成
- placeholder（PH）的展开（`ph_expand.py`）与写回编排
- `HarnessEvent` 有序事件流（`turn_started` / `model_delta` / `tool_started` / `tool_finished` / `cancelled` / `final` / `error`）
- run 级 usage 快照累积（`UsageAccumulator`）

## Does Not Own

- Protocol 渲染、provider HTTP 代码（`llm_driver`）
- 持久化 config 加载、session/agent 外层 API（`agents` / `agent_adapter`）
- 上下文持久化权威（`ActiveContextStore`）、durable log（`HistoryMirror`）、run 元数据（`state.RunStore`）——harness 只读写，不持有
- 手动 `/compact` 命令流（`context` SDK 侧），harness 只与之共享 `compact.py` 纯函数

## Inputs

- `HarnessRequest`（session_id / agent_name / user_input）
- `HarnessContext`（model、cwd、config_snapshot、abort、各 registry/store 引用）
- agent prompt/config（经 `config_snapshot` 传入）
- 已注册的工具、技能、记忆注入器、extension registry

## Outputs

- 有序 `HarnessEvent` 流（末事件 `final` 携带 `UsageSnapshot`；无 `TurnUsage` 类型）
- run 状态写回（`RunRecord` status / usage，经 `StateRepository`）
- PH 替换 payload（经 write_queue / MainWriter / 直写 store 三级回退）
- compact 摘要写回（boundary + `CompactSummary`，见 [harness_compact](harness_compact.md)）

## Replacement Rule

Any harness implementation is valid if the same request and context produce semantically equivalent events and state transitions.
