---
title: harness tool dispatch
type: doc
status: active
---

# harness tool dispatch

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [default.py](../src/xana/harness_sdk/default.py) — `_dispatch_tool()`（:215）、`_run_tool_with_abort()`（:161）、`_split_tool_schemas()`（:1155）
- [interfaces.py](../src/xana/harness_sdk/interfaces.py) — `ToolCallParser` / `ToolDispatcher` / `HarnessEventMapper` ABC（仅合约）
- [compact.py](../src/xana/harness_sdk/compact.py) — `revealed_tools_from_messages()`（揭示状态恢复）

## 职责

模型 tool_call 事件的缓冲、解析与执行，以及每轮 tools_schema 的构建（deferred 揭示机制）。

## 事件 → 调度的形状

`client.stream()` 的 `tool_call` 事件在流内只缓冲（tool_name / input / call_id），**中途不分发**——否则模型看不到工具结果（与 CC query loop 同款）。流出 done 后：无 tool_calls → turn 结束；有则逐个 `_dispatch_tool`，结果写成 role="tool" 的 `MemoryMessage`（metadata 含 tool_use_id / tool_name / ok，以及可选的 `revealed_tools`、`display_text`），loop 回下一轮。

## 单工具管线（`_dispatch_tool`）

`validate → check_permission → (approval_review) → pre_tool_use → execute → post_tool_use → render_result`，整条包在 try 里：任何异常变成 synthetic failure `ToolResult`（failure_kind="execution"），turn 继续，模型可自行应对。

- **查找优先级**：extension 贡献的工具（`ext_tools`）先于共享 `ToolRegistry`，且覆盖同名内置工具；两者都未命中 → failure_kind="unsupported" / "not_found" 的 synthetic 结果。
- **permission**：`check_permission` 返回非 allow 时，先经 extension `approval_review` contributors 逐个 `review()`，第一个非 None override 生效；仍非 allow → `_denied_result`（failure_kind="policy_forbidden"）。无 permission 上下文时给 session 级 default mode。
- **工具生命周期 hook**：extension `on_tool_start` / `on_tool_finish` 经 `_safe_hook` 包裹（异常只记日志）。

## 执行中止（`_run_tool_with_abort`）

`AbortSignal.aborted` 触发时：

1. 先调用 `tool.cancel(call_id)` 发协作式取消信号（一次）。
2. read_only 工具 → 立即 `task.cancel()`，返回 cancelled 结果（附"只读工具被取消，可重试"合成文案）。
3. 写工具 → `asyncio.wait_for(shield(task), 2.0s)` 宽限期；超时强杀并注入"可能被部分完成、切勿自动重试"的合成中文警告（`_TOOL_ABORT_GRACE_SECONDS = 2.0`）。

## 每轮 tools_schema 构建

- `_split_tool_schemas()`：`defer=True` 且未揭示的工具只进 deferred 名单（名字+描述进 system prompt），其余给完整 `{name, description, input_schema}`。
- 揭示状态存在历史消息 metadata 里（`revealed_tools` ∪ compact boundary 的 `pre_compact_revealed_tools`，CC preCompactDiscoveredTools 同思路），由 `revealed_tools_from_messages()` 恢复；工具可通过结果 metadata 追加揭示。
- extension 工具不参与 defer（永远全量 schema）。
- native web_search 互斥：`search_provider` 为 native 且 provider 是 deepseek 系时，函数版 web_search 从 schema 与 deferred 名单剔除，改经 request metadata 通知 provider 注入 server tool。
- `thinking` config → request metadata 方言开关。

## 仅接口合约，无实现

`ToolCallParser` / `ToolDispatcher` / `HarnessEventMapper` 三个 ABC 在 interfaces.py 定义，但**没有任何实现类**——实际的解析（流事件缓冲）、调度（`_dispatch_tool`）与事件映射（`_run_query_loop` 内联 yield）都内联在 `DefaultHarness` 里。
