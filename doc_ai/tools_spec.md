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
| `display` | `ToolDisplay` \| None | 默认显示配置（见下）；None = 渲染层兜底 |
| `yields_to_native` | bool | provider 有等价原生服务端能力时让位（如 deepseek 原生联网搜索）：harness 从 schema 剔除本工具、由 provider 注入原生能力；仅当本工具在该 agent 可用范围内时触发。目前只有旧版 web_search 声明 |
| `toolset` | str | 所属工具组（见下）；`""` = 未分组，只能按工具名或 `*` 选中 |

`concurrency` 已被 harness 消费：同一轮里连续的 `"parallel"` 调用成批并发执行（上限 10），其余独占执行，结果按模型发出顺序写回（`harness_sdk/tool_batches.py`，见 [harness_tool_dispatch](harness_tool_dispatch.md)）。当前声明 parallel 的：read / glob / grep / memory_search / web_search / web_open_url / web_fetch / deep_research / background_research。
注意：`interrupt_behavior` / `permission_policy` / `sandbox_policy` / `timeout_s` 目前主要是声明与合约位，harness 的分发循环（`DefaultHarness._dispatch_tool`）尚未消费它们；实际超时/取消行为由各工具自己在 `execute`/`cancel` 内实现（如 Bash 的 timeout 与 SIGTERM/SIGKILL）。

## toolset：工具组

`Toolset(name, description, default)`（`tools/contracts.py`）登记在工具目录里：`ToolRegistry.register_toolset()` / `list_toolsets()`。agent 的 `tools` 表达式按组选工具（`default` = 所有 `default=True` 组的工具，见 [agents_spec](agents_spec.md)）。内置组的权威表是 `tools/toolsets.py` 的 `BUILTIN_TOOLSETS`，由 `boot()` 登记；附加项注册工具时同时登记自己的组并决定是否默认——专用工具登记为非默认组，只有显式写了该组的 agent 才拿得到。

| 组 | 工具 | 默认 |
|---|---|---|
| fs | Read / Write / Edit / Glob / Grep | ✓ |
| exec | Bash | ✓ |
| web | web_search / web_open_url / web_fetch | ✓ |
| memory | memory_search / memory_write | ✓ |
| session | TodoWrite / EnterPlanMode / EnterWorktree / report_goal | ✓ |
| meta | tool_search / skill_load | ✓ |
| research | deep_research / background_research | ✓ |
| schedule | CronCreate / CronList / CronDelete | ✓ |

`tools/toolsets.py` 另有 `resolve_tools(selection, disallowed, specs, toolsets)`（表达式 → 工具名集合，纯函数，harness 每次 invoke 调一次）与 `unknown_names()`（boot 据此对 agent 里写错的组名 / 工具名记 warning）。

系统级禁用：`system.json` 的 `tools.disabled`（组名或工具名），`boot()` 统一经一个注册函数注册，被禁用的工具不进入工具目录，对所有 agent 都不存在。设计见 [agent-composition-design](../docs/design/agent-composition-design.md)。

## display：工具的默认显示配置

`ToolDisplay`（同在 `tools/contracts.py`）声明工具在渠道卡片里怎么显示，供显示模式分组、计数、生成标题（设计见 [tool-display-modes](../docs/design/tool-display-modes.md)）：

| 字段 | 语义 |
|---|---|
| `category` | `read / search_code / edit / execute / web_search / web_open / research / memory / plan / delegate / ask / other`；进行时文案（"正在打开网页"）由类别决定 |
| `verb` | 完成态动词，如 "搜索网页"、"打开"、"运行" |
| `unit` | 计数单位，如 "次"、"个网页"、"条命令" |
| `count_arg` | 按某个数组参数的长度计数（`web_open_url` 用 `urls`：一次打开 5 页记"打开 5 个网页"）；None = 每次调用计 1 |
| `summary_args` | 单行摘要取哪些参数（按优先级，列表参数用 " / " 连接） |
| `result` | 展开后显示多少结果：`none / preview / full` |

harness 在 `tool_started` 事件 payload 里带上 `display.to_payload(args)`（`DefaultHarness._tool_display`）。所有内置工具都已声明；MCP / extension 工具未声明时，渲染层按类别 other、动词 = 工具名、旧摘要优先级兜底。

## ToolInvocation / ToolContext

- `ToolInvocation`：harness 每次调用构造，含 `tool_name`、`input`、`call_id`、`session_id`、`run_id?`。`call_id` 是取消寻址键（`Tool.cancel(call_id)`）。
- `ToolContext`：会话环境快照，含 `cwd`（相对路径解析基准）、`permission`（`PermissionContext`）、`metadata`（供工具间传递，如 worktree 路径）、`report_progress`（执行中上报一句进度，如"正在阅读 5/12 个网页"；harness 转成 `tool_progress` 事件给渠道显示，不进模型上下文；None = 不接收）。

## defer 与按需工具（tool_search 两阶段）

`ToolSpec.defer=True` 的工具不进入常规 tools_schema：

1. **默认态**：harness 把 deferred 工具以「名字: 描述」一行形式写进 `deferred_tools` ContextNote（`context_notes.deferred_tools_note()`，追加进历史，名单变化才追加；清空且曾列出过时追加"均已加载"），system prompt 不含该名单；LLM API 的 tools_schema 里只有 `tool_search`，不见 deferred 工具的参数定义——模型物理上无法调用未揭示的工具。已揭示状态记在 tool 结果的 `revealed_tools` 上，经 SDK 往返保留，跨 run 有效。
2. **揭示**：模型调 `tool_search`，支持三种查询——`select:名字1,名字2` 精确选择；关键词评分搜索（名字命中 10/5 分，描述命中 2 分，返回前 `max_results` 个）；`+必须词` 前缀过滤。命中工具的完整 JSONSchema 以 `<functions>` 块返回。
3. **状态恢复**：`tool_search` 把命中名字写入 `ToolResult.metadata["revealed_tools"]`；harness 把它透传进 tool 消息的 `MemoryMessage.metadata`（`default.py` 分发循环）。后续每轮用 `revealed_tools_from_messages()` 扫历史恢复揭示集合，被揭示的工具以完整 schema 进入 tools_schema；compact 时揭示状态经 `pre_compact_revealed_tools` 写进 boundary metadata，压缩后不丢。

当前 `defer=True` 的工具：`cron_create` / `cron_list` / `cron_delete`，以及未配置 xana-search 时的旧 `web_search` / `web_fetch`（配置了 xana-search 时注册的 `web_search` / `web_open_url` / `deep_research` 不 defer）。extension 工具不参与 defer（永远全量 schema，且覆盖同名内置工具）。
