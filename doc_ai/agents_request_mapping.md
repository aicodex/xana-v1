---
title: agents request mapping
type: doc
status: active
---

# agents request mapping

## 上级模块

- [agents](agents.md) — 模块设计

## 职责

Map AgentRequest to HarnessRequest and HarnessContext.

## 相关接口 / 类型

- `AgentRequest`
- `AgentRequestMapper`
- `AgentEventMapper`

## 代码锚点

- [contracts.py](../src/xana/agents/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/agents/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [agents](./agents.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
