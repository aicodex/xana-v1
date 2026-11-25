---
title: llm_driver
type: doc
status: active
---

# llm_driver

## 定位

L1 模型驱动层：多 Provider 线协议传输（OpenAI Chat / Anthropic Messages / Kimi OAuth）、错误分类、SSE 流解析、tool call 提取、usage 统计，以及可替换的重试/恢复/改写/路由策略槽与 Stage 0 Tape 录制。不执行工具、不写 memory。

- `contracts.py`：冻结线协议与策略契约（受 `tests/golden/llm_driver_contract_v1.json` 锁定）。
- `interfaces.py`：`ModelClient`、`ModelRegistry`、`WireTransport`、`CredentialResolver` + 4 个可进化策略 ABC（`RetryPolicyStrategy`、`StreamRecoveryStrategy`、`RequestRewriteStrategy`、`RoutingStrategy`）。
- `driver.py` / `policies.py` / `transports/` / `providers/` / `auth/`：驱动主流程、默认策略实现、方言传输与鉴权解析（Kimi OAuth 在 `auth/kimi_oauth.py`）。
- `tape_recorder.py`：`LlmTapeRecorder` 直写 Stage 0 `TapeWriter`（`llm.request` / `llm.attempt` / `llm.outcome`）；内置可开关诊断日志 `DiagLogger("llm")`。
- `__main__.py`：独立 CLI `python -m xana.llm_driver bench`。

## 详细设计

- [modular_interfaces](modular_interfaces.md#2-stage-1llm-driversrcxanallm_driver) — L1 全部 ABC 签名与策略契约速查
- [llm_provider_contract](llm_provider_contract.md) — `ModelClient` / `ModelRegistry` 接口；providers/ 下真实 client 与 fake client 的装配、鉴权、能力声明
- [llm_streaming](llm_streaming.md) — `ModelEvent` 流契约、两个方言的 SSE 解析、DSML 泄漏恢复与流式 scrubber、fake client 回合行为
- [llm_tool_schema](llm_tool_schema.md) — tools_schema 在各方言间的转换（Anthropic 形状 ↔ OpenAI function tools）、native web_search server tool
- [llm_usage_errors](llm_usage_errors.md) — usage 提取、rate_limited / error 事件语义、`ProviderError` / `RetryPolicy` / `RateLimitSignal` 合约

## 全局合约

- [README_ai](README_ai.md) — L0 + 7 层架构总览
- [modular_interfaces](modular_interfaces.md) — 全系统接口文档
- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- `ModelRequest` → provider wire shape 的转换与 `WireTransport` 方言传输。
- provider SSE → `ModelEvent` 流的解析（text_delta / tool_call / usage / done）。
- DeepSeek Anthropic 方言的 DSML tool-call 泄漏恢复（服务端转换缺失时客户端补完）。
- Kimi OAuth device flow、token 存取与到期刷新（`auth/kimi_oauth.py`）。
- 请求级重试/流恢复/错误改写/多目标路由策略（经 4 个策略 ABC 注入）。
- 测试/离线用的 fake client、回放测试双（`testing.py`）与内存 registry。

## Does Not Own

- Turn loop、tool 执行、tool 结果回填（调用方负责）。
- effort / 预算策略（effort 值由调用方给，driver 只做方言映射）。
- 会话级持久化与 config 路径解析（由宿主注入）。

## Inputs

- `ModelRequest`（messages / tools_schema / effort / metadata）
- provider 配置（api_key、base_url、region、token store）

## Outputs

- `ModelEvent` 异步流（text_delta / tool_call / usage / done / rate_limited / error）
- `ProviderCapabilities`
