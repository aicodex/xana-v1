---
title: agent_adapter
type: doc
status: active
---

# agent_adapter

## 定位

CLI/TG/Feishu/HTTP 等外部协议翻译。

## 详细设计

- [agent_adapter_inbound](agent_adapter_inbound.md) — Parse external protocol payloads into InboundMessage.
- [agent_adapter_commands](agent_adapter_commands.md) — Command listing and routing.
- [agent_adapter_rendering](agent_adapter_rendering.md) — Draft open/update/close and protocol-specific rendered message.
- [agent_adapter_gateway](agent_adapter_gateway.md) — Register adapters, submit inbound messages, attach/detach foreground runs（subscribe 为 v1 未实现，见该文）.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

External protocol translation: CLI, Telegram, Feishu, HTTP, or SDK callers.

## Does Not Own

Agent decision logic, harness turn loop, memory, tools, or model provider calls.

## Inputs

- protocol-specific inbound messages
- `AgentEvent` stream for rendering

## Outputs

- `InboundMessage` to agent gateway
- rendered protocol-specific replies
