---
title: llm streaming
type: doc
status: active
---

# llm streaming

## 上级模块

- [llm_driver](llm_driver.md) — 模块设计

## 职责

`ModelClient.stream` 的输出契约：SSE 解析、事件顺序、DSML 泄漏恢复、fake client 回合行为。

## 相关接口 / 类型

- `ModelClient.stream`
- `ModelEvent` / `ModelEventKind`
- `StreamingContextScrubber`、`parse_dsml_tool_calls`（DSML 恢复）

## 代码锚点

- [contracts.py](../src/xana/llm_driver/contracts.py) — `ModelEvent`, `ModelEventKind`, `ModelUsage`
- [providers/anthropic_deepseek.py](../src/xana/llm_driver/providers/anthropic_deepseek.py) — Anthropic 方言 SSE 解析
- [providers/kimi.py](../src/xana/llm_driver/providers/kimi.py) — OpenAI 方言 SSE 解析
- [providers/dsml_recovery.py](../src/xana/llm_driver/providers/dsml_recovery.py) — DSML 解析
- [providers/stream_scrubber.py](../src/xana/llm_driver/providers/stream_scrubber.py) — 流式 scrubber
- [fake_client.py](../src/xana/llm_driver/fake_client.py) — 测试 client

## ModelEvent 契约

`ModelEventKind = "text_delta" | "tool_call" | "usage" | "done" | "error" | "rate_limited"`。

- text_delta：payload `{"text": ...}`；reasoning 文本带 `payload["channel"] == "thinking"`（复用 text_delta，不新增事件类型）。
- tool_call：payload `{"tool_name", "input", "call_id"}`。
- 成功 turn 的事件序：若干 text_delta / tool_call → 恰好一个 usage → done（done 永远最后）。
- HTTP 429 → rate_limited 事件后流直接结束，无 usage / done；error 事件同理。见 [llm_usage_errors](llm_usage_errors.md)。

## Anthropic 方言（anthropic_deepseek）

解析 `event:` / `data:` SSE 行，空行触发一次事件分发：

- content_block_start 按 index 登记 block（type/name/id）；content_block_delta 分三类——text_delta（正文）、thinking_delta / reasoning_content_delta（reasoning）、input_json_delta（按 index 累积 tool input 的 JSON 片段）。
- content_block_stop 对 tool_use block 发出单个 tool_call（partial_json 解析失败则 input={}）。
- message_start / message_delta 的 usage 逐字段合并（后到的非空值覆盖），键 input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens，映射到 `ModelUsage` 的 input/output/cache_read/cache_write。
- delta 同时兼容 OpenAI 拼写（reasoning_content / content）与 Anthropic 拼写（thinking / text）；两者同帧存在时 thinking 事件在前。

## OpenAI 方言（kimi）

- `data:` 行 JSON 载荷，`[DONE]` 结束。`stream_options.include_usage` 保证流尾带 usage：prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens → `ModelUsage` 的 input/output/cache_read。
- delta.tool_calls 按 index 聚合（id/name 只在首片出现，arguments 分片拼接）；finish_reason=="tool_calls" 时按 index 排序一次性发出 tool_call（arguments 解析失败则 input={}）。
- delta.reasoning_content / reasoning → `channel: "thinking"` 的 text_delta。

## DSML 泄漏恢复（DeepSeek 方言）

DeepSeek-V4 的 /anthropic 端点应把 DSML tool call 转成 tool_use block，但对 client function tools 转换不稳定（观察约 50% 泄漏进 text block）。客户端在流结束后补完服务端跳过的转换：

- 全程累积**原始**文本（text_buffer）；每个 text_delta 对外先过 `StreamingContextScrubber(DSML_FENCE_TAGS)`，泄漏的原始 DSML 标记不出现在用户可见文本里。
- SSE 结束后 `parse_dsml_tool_calls(text_buffer)`：要求 open + close fence 同时存在才算泄漏（防散文误报）；fence 标记用全角竖线 U+FF5C，单管（model card 形）与双管（endpoint 转义形）两种都接受。
- 恢复出的每个 call 发 tool_call 事件，call_id 取 `dsml-recovered-<hex>`；参数值默认按 JSON 解析，`string="true"` 时保留原始字符串，畸形 JSON 退化为字符串不抛异常。
- reasoning 不进 text_buffer，不参与恢复扫描；恢复命中记 warning（`DSML_LEAK_WORKAROUND`，含恢复出的工具名）。

## StreamingContextScrubber

有状态跨 chunk 清洗器（移植自 yuxu / Hermes）：按 (open, close) 标签对剥离 fenced span；匹配是字面子串、大小写不敏感，支持多标签对。open/close 可能分属不同 SSE delta，因此任何"可能是某标签前缀"的尾部片段都留缓冲，下一个 feed 或 flush 时再决定归属。flush 时若仍处在未闭合 span 内，剩余内容直接丢弃（截断比泄漏内部标记好）。每个 LLM 调用新建实例（或 reset），实例不跨流复用。

## FakeModelClient（测试 / 离线）

- `ScriptedTurn(text_deltas, tool_call, usage_input_tokens, usage_output_tokens)` 显式指定一轮输出：text_deltas 逐项发 text_delta，tool_call 发一个 tool_call，最后 usage + done。script 耗尽后再被调用只发 done（不发 usage，避免清零上一轮统计）。
- 默认 turn（空的 ScriptedTurn）配合 DefaultHarness 多轮工具循环：cursor=0 逐行回显组好的 prompt（DS wire 形），并发起一个 `memory_search` tool_call（query 取最后一条 user 消息，top_k=3，call_id=fake-call-1）；cursor>=1 流式输出收尾文本并带 `[fake-reply ok]` 标记，不再发 tool_call → harness 循环终止。
