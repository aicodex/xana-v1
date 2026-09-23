---
title: tools result
type: doc
status: active
---

# tools result

## 上级模块

- [tools](tools.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/tools/contracts.py) — `ToolResult`, `ToolArtifact`, `ToolDiff`, `ToolFailureKind`

## 职责

一次工具调用的结构化产出与失败归因。`render_result()` 产出的可读摘要进 active context；结构化字段供上层（UI、observability、resume 逻辑）消费。

## ToolResult 字段

| 字段 | 语义 |
|---|---|
| `ok` | 总成功标志 |
| `content` | 主内容（结构任意：dict / list / str；取消时是 synthetic warning 文本） |
| `structured_content` | 可选强类型输出 |
| `error` | 错误描述 |
| `failure_kind` | 失败归因（见下），仅 `ok=False` 时有意义 |
| `rendered` | 预渲染的一行摘要；`render_result()` 直接返回它 |
| `artifacts` | `ToolArtifact` 元组（产物文件引用，含 content_type/metadata） |
| `diffs` | `ToolDiff` 元组（path + unified diff 文本；`EditTool` 每次成功都产出） |
| `stdout` / `stderr` / `exit_code` | 子进程类工具的直接通道（`Bash`） |
| `truncated` | 输出被截断标记（Bash 50k 字符、Glob/Grep 250 条、web_fetch max_chars） |
| `retryable` | 上层可否重试（web_fetch/web_search 网络错误置位） |
| `metadata` | 透传 harness 的键值，关键约定：`revealed_tools`（tool_search 揭示状态）、`mode`（EnterPlanMode） |

## ToolFailureKind 十种归因

codex `ToolItemFailureKind` 超集（注释见 contracts.py 头部）：

- `validation` — `validate` 失败或入口校验拒绝（如 cron 表达式非法、web_fetch 非 http URL）
- `policy_forbidden` — 权限闸门 deny（`_denied_result` 唯一使用的值）
- `approval_denied` / `approval_aborted` — 合约预留：用户拒绝 / 审批超时，编排层暂未产生
- `sandbox_denied` — 沙箱拒绝（如写越出 writable_roots；合约预留）
- `timeout` — 超工具自身时限（Bash 超时杀）
- `execution` — 工具体内运行时错误 / 非零退出码 / 未捕获异常
- `cancelled` — abort 取消（read_only 立即 or write 宽限后强杀）
- `not_found` — 目标资源不存在（Read/Edit/Glob 文件缺失、skill_load 技能缺失）
- `unsupported` — 能力缺失（web_search native 模式、不支持的 content-type）

## 不变量

- `failure_kind` 只在 `ok=False` 时设置。
- harness 对 `validate`/生命周期各步抛出的未捕获异常统一兜成 `failure_kind="execution"` 的合成结果，调用链不中断。
- tool 消息（含 `metadata.revealed_tools` 等）写入 active context 与 history mirror，resume/compact 依赖这些 metadata。
