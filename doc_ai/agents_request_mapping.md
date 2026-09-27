---
title: agents request mapping
type: doc
status: active
---

# agents request mapping

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agents/contracts.py) — `AgentRequest` / `AgentControlRequest` / `AgentEvent`
- [interfaces.py](../src/xana/agents/interfaces.py) — `AgentRequestMapper` / `AgentEventMapper` ABC
- [chat_agent.py](../src/xana/agents/chat_agent.py) — `DefaultAgentRequestMapper` / `DefaultAgentEventMapper` 实现

## 职责

把面向会话的 `AgentRequest` 翻译成 harness 可执行的 `HarnessRequest` + `HarnessContext`，并把 harness 事件流映射回 `AgentEvent`。

## 契约

- `AgentRequest` — `session_id` / `text` / `source`（发起 adapter）/ `target_agent` / `attachments` / `metadata` / `tool_ceiling`（委派子 run 的工具上限，`frozenset[str] | None`，None = 不限；由 `GatewayAgentRunner` 填）。gateway 经 `metadata` 传入 `gateway_run_id` 和 `abort_signal`（见 [agent_adapter_gateway](agent_adapter_gateway.md)）。
- `AgentControlRequest` — `session_id` / `op` / `args`。
- `AgentEvent` — `kind`（`draft / tool / final / control_result / error / cancelled`）+ `session_id` + `payload` + 原始 `HarnessEvent`。

## DefaultAgentRequestMapper

构造注入各 registry/store（active_context_store、history_mirror、memory_injector_registry、tool/skill/model registry、state_repository、session_config_store 等）与运行参数（cwd、model、provider、阈值、writers）。

`to_harness_request`：直译——`user_input=request.text`，`agent_name=spec.component.name`，metadata 原样透传。

`to_harness_context`：

- `run_id` 取 `request.metadata["gateway_run_id"]`；没有则回退 `{session_id}-{uuid8}`（单测/直调场景）。这保证 adapter 端按 gateway run_id 建的渲染状态和 harness 事件对得上。
- 模型/effort/async_mode 以 `SessionConfigStore` 的会话配置覆盖默认值；`context_window` / `force_compact` 可被 `model_thresholds[model]` 按模型覆盖。
- `config_snapshot` 携带 `provider`、`agent_system_prompt`（AgentSpec.system_prompt 模板）、`tools` / `disallowed_tools`（AgentSpec 的工具选择表达式，harness 解析）、`tool_ceiling`（`AgentRequest.tool_ceiling` 排序后的列表；None = 无上限，harness 与解析结果取交集）、`skills`（AgentSpec.skills：`None` 或含 `*` → `None` = 全部可见，否则排序名单；harness 转成 `visible_skills`）、async/streaming/thinking/search_provider 等；后三者来自会话配置。
- `abort` 取 `request.metadata["abort_signal"]`（gateway 预注册的可变信号），否则新建实例。
- `metadata` 装配 `main_writer` / `run_jsonl_writer` / `write_queue` / `runs_dir`，供 harness 异步写回与 per-run JSONL 落盘。
- `permission` 目前恒为 `PermissionContext(mode="default", ...)`。

## DefaultAgentEventMapper

kind 映射：`final/error/cancelled` 同名透传，`tool_finished → tool`，其余一律 `draft`；payload 原样拷贝并保留原始 `HarnessEvent` 引用。

## 不变量

- mapper 不做 LLM 调用、不读写协议层状态；所有跨层对象（abort signal、run_id）只能经 metadata 传递，不新建隐藏权威。
- gateway 拥有 `AbortSignal` 生命周期（P5 修复）：mapper 复用 gateway 预注册的实例，保证 gateway.cancel() 原地翻转让在跑 turn 的工具轮询立即看到。
