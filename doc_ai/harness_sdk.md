---
title: harness_sdk
type: doc
status: active
---

# harness_sdk

## 定位

turn loop、暂停/恢复/取消、事件流和 usage 快照。

## 详细设计

- [harness_prompt_assembly](harness_prompt_assembly.md) — Build model messages from request, context, skills, and memory injection.
- [harness_turn_controller](harness_turn_controller.md) — Own run status transitions, checkpoints, pause/resume/cancel/finish/fail.
- [harness_tool_dispatch](harness_tool_dispatch.md) — Parse model tool calls and dispatch through tool lifecycle.
- [harness_usage](harness_usage.md) — Estimate and accumulate provider usage as turn properties.
- [harness_core](harness_core.md) — Top-level Harness.invoke event stream contract.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Turn lifecycle, prompt assembly boundary, LLM call orchestration, tool dispatch, pause/resume/cancel, and turn usage snapshots.

## Does Not Own

Protocol rendering, provider-specific HTTP code, persistent config loading, or agent-specific commands.

## Inputs

- `HarnessRequest`
- `HarnessContext`
- agent prompt/config
- registered tools and skills

## Outputs

- ordered `HarnessEvent` stream
- `TurnUsage`
- turn status transitions

## Replacement Rule

Any harness implementation is valid if the same request and context produce semantically equivalent events and state transitions.
