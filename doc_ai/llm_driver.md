---
title: llm_driver
type: doc
status: active
---

# llm_driver

## 定位

模型请求的发出与响应解析：provider 方言转换、SSE 流解析、tool call 提取、usage 统计。不执行工具、不写 memory。

## 详细设计

- [llm_provider_contract](llm_provider_contract.md) — `ModelClient` / `ModelRegistry` 接口；providers/ 下两个真实 client（anthropic_deepseek、kimi）+ fake client 的装配、鉴权、能力声明
- [llm_streaming](llm_streaming.md) — `ModelEvent` 流契约、两个方言的 SSE 解析、DSML 泄漏恢复与流式 scrubber、fake client 回合行为
- [llm_tool_schema](llm_tool_schema.md) — tools_schema 在各方言间的转换（Anthropic 形状 ↔ OpenAI function tools）、native web_search server tool
- [llm_usage_errors](llm_usage_errors.md) — usage 提取、rate_limited / error 事件语义、`ProviderError` / `RetryPolicy` / `RateLimitSignal` 合约

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- `ModelRequest` → provider wire shape 的转换。
- provider SSE → `ModelEvent` 流的解析（text_delta / tool_call / usage / done）。
- DeepSeek Anthropic 方言的 DSML tool-call 泄漏恢复（服务端转换缺失时客户端补完）。
- kimi OAuth device flow、token 存取与到期刷新（providers/kimi_oauth.py）。
- 测试/离线用的 fake client 与内存 registry。

## Does Not Own

- Turn loop、tool 执行、tool 结果回填（调用方负责）。
- effort / 预算策略（effort 值由调用方给，driver 只做方言映射）。
- 限流重试决策（driver 只发 `rate_limited` / `error` 信号，不重试）。

## Inputs

- `ModelRequest`（messages / tools_schema / effort / metadata）
- provider 配置（api_key、base_url、region、token store）

## Outputs

- `ModelEvent` 异步流（text_delta / tool_call / usage / done / rate_limited / error）
- `ProviderCapabilities`
