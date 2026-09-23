---
title: tools spec
type: doc
status: active
---

# tools spec

## 上级模块

- [tools](tools.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/tools/contracts.py) — `ToolSpec`, `ToolInvocation`, `ToolContext`, `ToolArtifact`, `ToolDiff`
- [interfaces.py](../src/xana/tools/interfaces.py) — `Tool`（spec() 返回 `ToolSpec`）

## 职责

`ToolSpec` 是工具的唯一声明入口：metadata（借 `ComponentSpec`）、输入/输出 schema、执行属性（并发/超时/截断）、权限/沙箱引用，以及 deferred 隐藏标记。

## ToolSpec 字段

| 字段 | 类型 | 语义 |
|---|---|---|
| `component` | `ComponentSpec` | 工具身份（kind="tool"、name、qualified_name、description、source） |
| `input_schema` | dict | JSON Schema，直接进 LLM tools_schema |
| `output_schema` | dict \| None | 可选输出 schema |
| `read_only` | bool | 取消语义开关：`True` → cancel 立即 abort；`False` → 2 秒宽限后强杀（见 [tools_lifecycle](tools_lifecycle.md)） |
| `destructive` | bool | 是否修改外部状态（声明性，供 policy/UI 参考） |
| `open_world` | bool | 是否触达外部世界（网络/子进程） |
| `concurrency` | `"parallel" \| "serialized" \| "exclusive"` | 并发声明，默认 `serialized` |
| `interrupt_behavior` | `"cancel" \| "block" \| "ignore"` | 中断声明，默认 `block` |
| `permission_policy` | str \| None | 引用命名权限策略 |
| `sandbox_policy` | `SandboxPolicySpec` \| None | 沙箱要求（mode/writable_roots 等，来自 policy 模块） |
| `timeout_s` | float \| None | 执行超时 |
| `max_result_chars` | int | 结果截断阈值，默认 50_000 |
| `user_facing_name` | str \| None | UI 展示名 |
| `defer` | bool | 隐藏工具标记（见下） |

注意：`concurrency` / `interrupt_behavior` / `permission_policy` / `sandbox_policy` / `timeout_s` 目前主要是声明与合约位，harness 的分发循环（`DefaultHarness._dispatch_tool`）尚未消费它们；实际超时/取消行为由各工具自己在 `execute`/`cancel` 内实现（如 Bash 的 timeout 与 SIGTERM/SIGKILL）。

## ToolInvocation / ToolContext

- `ToolInvocation`：harness 每次调用构造，含 `tool_name`、`input`、`call_id`、`session_id`、`run_id?`。`call_id` 是取消寻址键（`Tool.cancel(call_id)`）。
- `ToolContext`：会话环境快照，含 `cwd`（相对路径解析基准）、`permission`（`PermissionContext`）、`metadata`（供工具间传递，如 worktree 路径）。

## defer 与按需工具（tool_search 两阶段）

`ToolSpec.defer=True` 的工具不进入常规 tools_schema：

1. **默认态**：harness 每轮把 deferred 工具以「名字: 描述」一行形式写进 system prompt 的「# 按需工具」段（`deferred_tools_section()`，模板 AGENT_LOOP_PROMPT.md），LLM API 的 tools_schema 里只有 `tool_search`，不见 deferred 工具的参数定义——模型物理上无法调用未揭示的工具。
2. **揭示**：模型调 `tool_search`，支持三种查询——`select:名字1,名字2` 精确选择；关键词评分搜索（名字命中 10/5 分，描述命中 2 分，返回前 `max_results` 个）；`+必须词` 前缀过滤。命中工具的完整 JSONSchema 以 `<functions>` 块返回。
3. **状态恢复**：`tool_search` 把命中名字写入 `ToolResult.metadata["revealed_tools"]`；harness 把它透传进 tool 消息的 `MemoryMessage.metadata`（`default.py` 分发循环）。后续每轮用 `revealed_tools_from_messages()` 扫历史恢复揭示集合，被揭示的工具以完整 schema 进入 tools_schema；compact 时揭示状态经 `pre_compact_revealed_tools` 写进 boundary metadata，压缩后不丢。

当前 `defer=True` 的工具：`cron_create` / `cron_list` / `cron_delete` / `web_search` / `web_fetch`。extension 工具不参与 defer（永远全量 schema，且覆盖同名内置工具）。
