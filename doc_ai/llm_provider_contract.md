---
title: llm provider contract
type: doc
status: active
---

# llm provider contract

## 上级模块

- [llm_driver](llm_driver.md) — 模块设计

## 职责

`ModelClient` / `ModelRegistry` 抽象与 provider 装配：谁被实例化、如何鉴权、声明什么能力。

## 相关接口 / 类型

- `ModelClient`（ABC）— `stream` / `cancel` / `capabilities`
- `ModelRegistry`（ABC）— `register` / `get`
- `ProviderCapabilities` — provider 能力声明

请求构建 / 响应解析 / 工具 schema 转换 / usage 解析都是各 client 内部私有函数，不设独立 ABC。

## 代码锚点

- [contracts.py](../src/xana/llm_driver/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/llm_driver/interfaces.py) — 抽象接口
- [providers/anthropic_deepseek.py](../src/xana/llm_driver/providers/anthropic_deepseek.py) — `AnthropicDeepSeekClient` / `AnthropicDeepSeekConfig`
- [providers/kimi.py](../src/xana/llm_driver/providers/kimi.py) — `KimiClient` / `KimiConfig`
- [providers/kimi_oauth.py](../src/xana/llm_driver/providers/kimi_oauth.py) — device flow + `KimiTokenStore`
- [fake_client.py](../src/xana/llm_driver/fake_client.py) — `FakeModelClient` / `InMemoryModelRegistry` / `ScriptedTurn`

## 接口语义

- `ModelClient.stream(request)` → `AsyncIterator[ModelEvent]`，事件契约见 [llm_streaming](llm_streaming.md)。
- `ModelClient.cancel(request_id)`：当前所有实现均为 no-op。
- `ModelClient.capabilities()`：两个真实 client 均声明 streaming/tools=True、cache_control=False、`max_output_tokens = config.max_tokens`。
- `ModelRegistry.register(provider, client)` / `get(provider)`：内存字典实现为 `InMemoryModelRegistry`；boot 时只注册 `default_model.provider` 一个 client。
- **per-provider 并发位**：`InMemoryModelRegistry(concurrency_limits={provider: max_concurrent})` 为每个 provider 建 `asyncio.Semaphore`；`get(provider)` 时若该 provider 有正数 limit，懒创建 semaphore 并用 `_ConcurrencyGuardedClient` 包装 client，`stream` 包在 semaphore 内（同 provider 并发超限的请求排队等空闲并发位）。boot 把 `SystemConfig.limit().max_concurrent` 逐项喂进来（boot.py:287-291）；未配置的 provider 直通原始 client（零行为变更）。

## Provider 装配（boot 唯一组合点）

`runtime/boot.py` 的 `_build_model_client` 按 provider 名选择 client，缺配置时回落 fake：

- 名字含 `"deepseek"`（同时命中 "anthropic_deepseek" 和用户手填的 "deepseek-v4-flash"）→ `AnthropicDeepSeekClient`；system.json 中该 model entry 缺失或 api_key 为空 → 警告并回落 fake。
- 名字以 `"kimi"` 开头 → `KimiClient` + `KimiTokenStore`；未登录 → 警告指引 `xana login` 并回落 fake。base_url 取 system.json entry 的 base_url，缺省用 `api_base(tokens.region)`。
- 其他 provider → fake。

## AnthropicDeepSeekClient

DeepSeek V4 走 Anthropic `/v1/messages` 方言，默认 base `https://api.deepseek.com/anthropic`。

- 鉴权：`Authorization: Bearer <api_key>`，header `anthropic-version: 2023-06-01`；httpx.AsyncClient 懒创建，测试可注入 `http_client`。
- body 关键项：`max_tokens`（默认 16384）、`stream: true`；全部 system 消息按 `"\n\n"` join 成 `body["system"]`。
- assistant 历史带 `metadata["tool_calls"]` 时渲染为 content blocks：最前可带 thinking block（`metadata["reasoning_content"]`，DS 要求工具调用轮次回传，否则后续请求 400），随后 text block 与每 call 一个 tool_use block。
- 相邻 `role="tool"` 消息合并为一个 user 消息，content 是 tool_result block 列表（Anthropic 要求同一轮 tool_use 的 tool_result 同处一个 user turn）。
- effort 方言：`body["output_config"] = {"effort": "high"|"max"}`；兼容映射 low/medium→high、high→high、xhigh→max、max→max；映射外的值记 warning 后跳过（避免 /effort 看似生效实则无效）。
- `request.metadata["thinking"]` ∈ {"disabled", "enabled"} → `body["thinking"] = {"type": ...}`（compact 摘要调用必须 disabled）；缺省不发，保持原行为。
- DSML 泄漏恢复见 [llm_streaming](llm_streaming.md)。

## KimiClient

k3 / kimi-for-coding 走 OpenAI chat-completions 方言，默认 base `https://api.kimi.com/coding/v1`；复刻 kimi-code `providers/kimi.ts` 的关键行为：

- 只发 `max_completion_tokens`（默认 16384），不发 `max_tokens`——reasoning 模型上 max_tokens 与 reasoning_content 共享预算，给小了会 200 空 content。
- 鉴权：`Authorization: Bearer <oauth access_token>`，token 由 `KimiTokenStore.access_token()` 供给（到期自动刷新）。
- thinking：body 级 `thinking: {"type": "enabled", "effort"?}`；调用方要求 disabled 时省略该字段（k3/kimi-for-coding 是 thinking-only 模型，省略比显式 disabled 稳妥，compact 场景不硬关）。
- effort 映射：low→low、medium→high、high→high、xhigh→max、max→max；映射外的值记 warning 后跳过。
- 401 → 强制刷新 token 重试一次（kimi-code 同款）；第二个 401 才报 auth 错误，消息引导 `xana login`。
- assistant 历史回传 `reasoning_content`；tool_calls 渲染为 OpenAI tool_calls（arguments 为 JSON 字符串）；`role="tool"` 一对一映射为 `{role: "tool", tool_call_id, content}`（OpenAI 允许连续 tool 消息，无需像 Anthropic 方言那样合并）。

## KimiTokenStore / OAuth（providers/kimi_oauth.py）

- device flow：全 POST form-urlencoded 到 OAuth host，只传 client_id（无 scope）。`POST /api/oauth/device_authorization` 拿 user_code/device_code/interval → 用户授权后轮询 `POST /api/oauth/token`（grant_type=device_code）：authorization_pending 继续等，slow_down 间隔 +5s，expired_token / access_denied / 超时 → `KimiAuthError`；轮询期间网络抖动不中断登录。
- token 存 `$XANA_HOME/credentials/kimi.json`（默认 `~/.xana/credentials/kimi.json`），权限 0600，tmp+replace 原子写；损坏 JSON 静默视为未登录。
- `access_token()`：未到期（60s margin）直接返回缓存；到期或 `force_refresh=True` 用 refresh_token 换新并落盘；无 refresh_token 或刷新失败 → `KimiAuthError`（消息引导 `xana login`）。`save_payload` 在响应缺 refresh_token 时保留旧值。
- 区域：`mainland-cn`（auth.kimi.com / api.kimi.com/coding/v1）与 `global`（auth.kimi.ai / api.kimi.ai/coding/v1）；`resolve_region()` 取 显式参数 > `$XANA_KIMI_REGION` > mainland-cn，未知区域抛 ValueError；`$XANA_KIMI_OAUTH_HOST` / `$XANA_KIMI_API_BASE` 可覆盖 host。
- `fetch_models(region, access_token)`：`GET {api_base}/models` 拉模型列表（每项含 id / context_length / supports_reasoning 等）。

## 不变量

- driver 不跑工具、不写 memory；tool_call 事件原样携带 name/input/call_id，执行与结果回填是调用方的职责。
- 缺 api_key / 未登录时回落 fake client 是显式行为（boot 打警告），不是静默成功。
