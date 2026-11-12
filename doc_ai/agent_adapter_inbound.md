---
title: agent adapter inbound
type: doc
status: active
---

# agent adapter inbound

## 上级模块

- [agent_adapter](agent_adapter.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agent_adapter/contracts.py) — `InboundMessage`
- [interfaces.py](../src/xana/agent_adapter/interfaces.py) — `AgentRequestRouter` ABC
- [router.py](../src/xana/agent_adapter/router.py) — `DefaultAgentRequestRouter`
- [feishu_events.py](../src/xana/agent_adapter/channels/feishu_events.py) — 飞书消息事件解析
- [telegram.py](../src/xana/agent_adapter/channels/telegram.py) — TG long-polling 入站循环

## 职责

把外部协议负载归一化为 `InboundMessage`，再路由为 [agents](agents.md) 层的 `AgentRequest`。

## 契约

`InboundMessage`（frozen）：`adapter_id`（来源 adapter，如 `cli` / `telegram` / `feishu`）、`session_id`、 `text`、`user_id`、`attachments`、`metadata`。gateway 与 router 之间的所有跨层数据（`chat_id`、`gateway_run_id`、`abort_signal`、`command_run_id`）都走 metadata，不扩契约字段。

## 协议解析

不设独立的解析 ABC：各 channel adapter 在自己的事件循环里直接解析协议负载（飞书 `parse_message_event` 把 im.message.receive_v1 事件转成 InboundMessage；TG 在 long-polling 循环里处理 Update），解析结果投入 adapter 内部队列，`receive()` 迭代产出。

## DefaultAgentRequestRouter

v1 唯一的 `AgentRequestRouter` 实现：把 `InboundMessage` 直译为 `AgentRequest`（session_id/text/attachments/metadata 原样，`source=adapter_id`），`target_agent` 由会话绑定的 agent 决定，未设置时使用构造默认值。

## 不变量

- adapter 只做协议翻译，不决定 agent 选择逻辑（v1 只有单一 chat agent，选择退化为常量）。
- 未通过 pairing 校验的用户消息在 adapter 内被拦下，不产生 InboundMessage（见 [agent_adapter_rendering](agent_adapter_rendering.md) 的 PairingStore）。
