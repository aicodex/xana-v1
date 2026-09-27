---
title: tools lifecycle
type: doc
status: active
---

# tools lifecycle

## 上级模块

- [tools](tools.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/tools/interfaces.py) — `Tool` ABC（spec + 七个生命周期方法）
- [default.py](../src/xana/harness_sdk/default.py) — `DefaultHarness._dispatch_tool` / `_run_tool_with_abort`（编排者）

## 职责

定义一次工具调用的完整生命周期，以及取消（cancel/abort）的协作式语义。编排者是 harness 的 `_dispatch_tool`；工具实现只按合约提供各阶段行为。

## 调用链

`DefaultHarness._dispatch_tool` 按序执行（extensions 先触发 `on_tool_start`）：

1. **解析**：先查 extension 工具表（`ext_tools`），miss 再查共享 `ToolRegistry`；都 miss 返回 `failure_kind="not_found"`。构造 `ToolInvocation` 与 `ToolContext`。
2. **`validate(invocation)`**：输入校验，抛异常 → `failure_kind="execution"` 的合成失败结果（异常被捕获，不进 LLM 上下文原文）。
3. **`check_permission(invocation, context)`**：工具级闸门，返回 `PermissionResult`。decision ≠ allow 时先给 extension 的 `approval_review` contributors 一次覆写机会（第一个非 None override 生效）；仍非 allow → `_denied_result`，不执行。
4. **`pre_tool_use(invocation, context)`**：可返回改写后的 invocation（如注入默认值）。当前所有内置工具都是恒等返回。
5. **`execute(prepared, context)`**：包在 `_run_tool_with_abort` 里跑（见下）。
6. **`post_tool_use(invocation, context, result)`**：结果后处理，可改写/拒绝结果。典型用法：`WriteTool` 在成功写入后对 memory tree root 内的 `.md` 文件做增量 reindex（`_reindex.maybe_reindex_markdown`）。
7. **`render_result(result)`**：产出一行人类可读摘要，同时作为 tool 消息内容写入 active context 与 history mirror。
8. extensions 触发 `on_tool_finish`（含 ok/error/rendered）。

任一步骤抛未捕获异常 → 捕获为 `failure_kind="execution"` 的合成 `ToolResult`（rendered = `[tool failed: ...]`），调用链不中断。

## 取消语义（`_run_tool_with_abort`）

`execute` 包成独立 asyncio task，harness 轮询 `abort.aborted`（20ms 粒度）：

- abort 触发后**先**调一次 `tool.cancel(call_id)` 做协作式清理（只发一次），然后按 `ToolSpec.read_only` 分叉：
  - **read_only=True**：立即 `task.cancel()`，返回 `failure_kind="cancelled"`、content 为 synthetic warning（`_SYNTHETIC_WARNING_TEXT_READONLY`）。
  - **read_only=False**：`asyncio.shield(task)` 等 2.0 秒宽限；完成则正常采用结果（写生效），超时 `task.cancel()` 强杀并返回 `failure_kind="cancelled"`、content 为 P5.2 synthetic 中文 warning。
- 实现协作式取消的代表：`BashTool` 内部维护 `call_id → subprocess` 表，`cancel()` 发 SIGTERM → 等 2 秒 → SIGKILL；execute 里的 `CancelledError` 分支兜底强杀。

resume 语义（P5.1）：read_only 工具取消后 resume = 重跑该工具；write 工具取消后 resume = fork 新 run。

## Tool ABC：必须实现与默认实现

`Tool` 只有三个抽象方法，每个工具必须实现：`spec()`、`check_permission()`（每个工具都要明确表态，不给默认）、`execute()`。其余钩子带透传默认实现，需要时才覆盖：

- `validate` → 不做额外校验（返回 None）；
- `pre_tool_use` → 原样返回 invocation；
- `post_tool_use` → 原样返回 result；
- `render_result` → 返回 `result.rendered or ""`；
- `cancel` → no-op。

tools 模块不再拆分各阶段的独立策略 ABC，编排直接走 `Tool` 方法。
