---
title: tools
type: doc
status: active
---

# tools

## 定位

工具校验、权限、沙箱、执行边界和输出 schema。

## 详细设计

- [tools_spec](tools_spec.md) — Tool metadata, schema, concurrency, timeout, permission and sandbox declarations.
- [tools_permission_sandbox](tools_permission_sandbox.md) — Permission and sandbox policy seams before execution.
- [tools_lifecycle](tools_lifecycle.md) — Validate, pre hook, execute, post hook, render, cancel.
- [tools_result](tools_result.md) — Structured result, artifacts, diffs, stdout/stderr, failure attribution.
- [tools_registry](tools_registry.md) — Tool registration and lookup.

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Tool metadata, input validation, permission checks, sandbox requirements, execution boundary, and output schema.

## Does Not Own

LLM turn loop, agent policy, adapter rendering, or long-term memory.

## Inputs

- `ToolInvocation`
- `ToolContext`

## Outputs

- `ToolResult`
- progress events
- permission requests
