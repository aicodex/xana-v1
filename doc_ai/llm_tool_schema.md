---
title: llm tool schema
type: doc
status: active
---

# llm tool schema

## 上级模块

- [llm_driver](llm_driver.md) — 模块设计

## 职责

`ModelRequest.tools_schema` 到各 provider 方言的转换。

## 代码锚点

- [contracts.py](../src/xana/llm_driver/contracts.py) — `ModelRequest.tools_schema`
- [providers/anthropic_deepseek.py](../src/xana/llm_driver/providers/anthropic_deepseek.py) — Anthropic 方言透传
- [providers/kimi.py](../src/xana/llm_driver/providers/kimi.py) — `_convert_tools`

## 真相

模块内 tools_schema 的权威形状是 Anthropic 风格 `{name, description, input_schema}`（`ModelRequest.tools_schema: tuple[dict]`），由各 provider 私有函数各自转换：

- anthropic_deepseek：原样透传（`body["tools"] = [dict(t) for t in tools_schema]`）——调用方需直接给 Anthropic 形状。
- kimi 的 `_convert_tools`：转成 OpenAI function tools——`{"type": "function", "function": {"name", "description", "parameters": input_schema}}`；input_schema 缺失时补 `{"type": "object", "properties": {}}`。

转换是各 client 内部私有函数，不设独立的 schema 适配 ABC。

## Native web_search（server tool）

harness 在互斥剔除函数版 web_search 之后，经 `request.metadata["web_search_native"]` 通知 anthropic_deepseek 在 tools 末尾追加 server tool，verbatim 形状、不过工具转换器：

```json
{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
```

v1 明确不做：native web_search 的服务端重跑（drop policy）、系统块 cache_control、工具结果消息转换。
