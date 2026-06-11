---
title: agent adapter rendering
type: doc
status: active
---

# agent adapter rendering

## 上级模块

- [agent_adapter](agent_adapter.md) — 模块设计

## 职责

Draft open/update/close and protocol-specific rendered message.

## 相关接口 / 类型

- `DraftHandle`
- `RenderedMessage`
- `DraftRenderer`

## 代码锚点

- [contracts.py](../src/xana/agent_adapter/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/agent_adapter/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [agent_adapter](./agent_adapter.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
