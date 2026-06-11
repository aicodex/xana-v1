---
title: tools permission sandbox
type: doc
status: active
---

# tools permission sandbox

## 上级模块

- [tools](tools.md) — 模块设计

## 职责

Permission and sandbox policy seams before execution.

## 相关接口 / 类型

- `ToolPermissionPolicy`
- `ToolSandboxPolicy`
- `ToolContextBuilder`

## 代码锚点

- [contracts.py](../src/xana/tools/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/tools/interfaces.py) — 抽象接口

## 当前状态

TODO: 当前是设计占位。实现时必须先更新本详细设计，再落代码和测试。

## 验收要求

- 输入/输出与 [tools](./tools.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
