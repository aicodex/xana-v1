---
title: hooks turn model
type: doc
status: active
---

# hooks turn model

## 上级模块

- [hooks](hooks.md) — 模块设计

## 职责

Pre/post turn and pre/post model request hooks.

## 相关接口 / 类型

- `PreTurnHook`
- `PostTurnHook`
- `PreModelRequestHook`
- `PostModelResponseHook`

## 代码锚点

- [contracts.py](../src/xana/hooks/contracts.py) — 数据结构和枚举
- [interfaces.py](../src/xana/hooks/interfaces.py) — 抽象接口

## 当前状态

仅接口合约，无实现。`src/xana/hooks/` 下只有 contracts.py / interfaces.py；`HarnessContext.hook_runner` 字段存在（`src/xana/harness_sdk/contracts.py:80`）但无人消费，无 `HookRunner` 实现。

## 验收要求

- 输入/输出与 [hooks](./hooks.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
