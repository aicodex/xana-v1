---
title: llm provider contract
type: doc
status: active
---

# llm provider contract

## 上级模块

- [llm_driver](llm_driver.md) — 模块设计

## 职责

Provider capabilities, request serialization, model registry.

## 相关接口 / 类型

- `ProviderCapabilities`
- `ProviderRequest`
- `ModelRegistry`

## 代码锚点

- [contracts.py](../src/xana/llm_driver/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/llm_driver/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [llm_driver](./llm_driver.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
