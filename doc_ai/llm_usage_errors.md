---
title: llm usage errors
type: doc
status: active
---

# llm usage errors

## 上级模块

- [llm_driver](llm_driver.md) — 模块设计

## 职责

usage 精确提取与错误/限流信号：`ModelUsage`、`ProviderError`、`RateLimitSignal`、`RetryPolicy`。

## 相关接口 / 类型

- `ModelUsage`
- `ProviderError` / `ProviderErrorKind`
- `RateLimitSignal`
- `RetryPolicy` — 仅合约字段，当前无消费者
- `UsageParser`（ABC）— 仅接口合约，无实现

## 代码锚点

- [contracts.py](../src/xana/llm_driver/contracts.py) — 上述数据合约
- [providers/anthropic_deepseek.py](../src/xana/llm_driver/providers/anthropic_deepseek.py) — Anthropic usage 合并、错误映射
- [providers/kimi.py](../src/xana/llm_driver/providers/kimi.py) — OpenAI usage、401 强制刷新重试

## usage 提取

- anthropic_deepseek：message_start / message_delta 的 usage 逐字段合并（后到的非空值覆盖），四键 input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens → `ModelUsage` 的 input/output/cache_read/cache_write。
- kimi：流尾单个 usage 对象（靠 `stream_options.include_usage`）：prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens → input/output/cache_read。
- 两个方言成功后各发恰好一个 usage 事件再 done；429 / HTTP 错误路径没有 usage。
- `cost_usd` 当前两个 provider 都不填，恒为 None。

## ProviderError 语义

`ProviderErrorKind = "rate_limit" | "auth" | "bad_request" | "server_error" | "timeout" | "cancelled" | "unknown"`；`ProviderError{kind, message, retryable, request_id, raw_status, raw}`。

- HTTP 状态映射（两个 client 一致）：401/403 → auth；400/422 → bad_request；>=500 → server_error 且 retryable=True；429 不走 error 事件，走 rate_limited。
- 网络错误（httpx.RequestError）：TimeoutException → timeout，其余 → unknown，均 retryable=True。
- kimi 401：token 强制刷新后原请求重试一次；刷新本身失败或第二个 401 → `ProviderError(kind="auth", retryable=False, raw_status=401)`，消息引导重新 `xana login`。未登录（取 token 即失败）也走同一 auth 错误形状。
- anthropic_deepseek 没有 401 刷新逻辑：api_key 失效直接报 auth 错误。
- `request_id` 当前两个实现都不填；`raw` 保留原始载荷供上层排查。

## RateLimitSignal / RetryPolicy

- 429 时发 rate_limited 事件：`RateLimitSignal{retry_after_s: Retry-After header（可解析时）, reason: "HTTP 429: <body 前 200 字符>"}`，随后流结束。
- driver 本身不重试：发完 rate_limited / error 事件流即收尾；是否重试、等多久由上层决定。事件级 retryable 标记（>=500 / 网络 True，4xx False）是给上层的决策输入。
- `RetryPolicy`（`ModelRequest.retry_policy` 字段，默认 max_retries=2、retry_on_rate_limit/server_error=True、base_delay_s=1.0）当前没有实现消费，是预留合约。

## 不变量

- usage 只在成功路径的流尾出现一次；fake client 在 script 耗尽后的 no-op 轮不发 usage（避免清零上一轮统计）。
- 错误消息截断：429 body 前 200 字符，其他 HTTP 错误 body 前 300 字符。
