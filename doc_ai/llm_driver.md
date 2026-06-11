---
title: llm_driver
type: doc
status: active
---

# llm_driver

## 定位

模型请求、流式响应、tool call 解析、usage 提取。

## 详细设计

- [llm_provider_contract](llm_provider_contract.md) — Provider capabilities, request serialization, model registry.
- [llm_streaming](llm_streaming.md) — Provider event parsing and ModelClient stream contract.
- [llm_tool_schema](llm_tool_schema.md) — Adapt tool schema to provider dialect.
- [llm_usage_errors](llm_usage_errors.md) — Exact usage extraction, retry, rate-limit and provider error signals.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Provider request construction, provider response parsing, streaming chunks, tool-call parsing, and exact usage extraction.

## Does Not Own

Turn loop, tool dispatch, memory writes, budget policy, or adapter rendering.

## Inputs

- `ModelRequest`
- provider config

## Outputs

- `ModelEvent` stream
- `ModelResponse`
- provider exact usage
