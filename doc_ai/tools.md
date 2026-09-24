---
title: tools
type: doc
status: active
---

# tools

## 定位

工具声明（spec）、输入校验、权限/沙箱接缝、执行边界、取消语义与结构化结果。执行编排由 harness 负责（见 [harness_tool_dispatch](harness_tool_dispatch.md)），本模块只定义 Tool 合约和内置实现。

## 详细设计

- [tools_spec](tools_spec.md) — `ToolSpec` / `ToolInvocation` / `ToolContext` 声明字段与 `defer` 隐藏工具机制
- [tools_permission_sandbox](tools_permission_sandbox.md) — `check_permission` 工具级接缝、受保护目录门禁、扩展 approval_review 覆写
- [tools_lifecycle](tools_lifecycle.md) — validate → check_permission → pre_tool_use → execute → post_tool_use → render_result → cancel 全链路
- [tools_result](tools_result.md) — `ToolResult` 结构化结果与 `ToolFailureKind` 十种失败归因
- [tools_registry](tools_registry.md) — `ToolRegistry` / `InMemoryToolRegistry` 注册与查找

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- Tool metadata（`ToolSpec`，含 JSON schema、并发、超时、权限/沙箱声明）。
- 输入校验（`Tool.validate`）与工具级权限闸门（`Tool.check_permission`）。
- 执行与取消语义（`Tool.execute` / `Tool.cancel`，read_only 立即取消 vs write 2 秒宽限）。
- 结构化结果与失败归因（`ToolResult` / `ToolFailureKind`）。
- 18 个内置工具实现（`src/xana/tools/builtin/`，共 17 个工具文件、19 个工具类，cron 三工具同文件）。

## Does Not Own

- LLM turn loop、agent policy、adapter 渲染、长期记忆持久化。
- 全局权限模式裁决（`policy` 模块）；工具只暴露 `check_permission` 接缝，编排是 harness 的事。

## 内置工具清单

全部在 [boot()](../src/xana/runtime/boot.py) 注册进同一个 `InMemoryToolRegistry`（17 个工具文件，cron 三工具同文件，共 19 个工具类）。`defer=True` 的工具默认只对模型暴露名字，经 `tool_search` 揭示后下轮获得完整 schema（见 [tools_spec](tools_spec.md)）。

| 工具名 | 文件 | 说明 | 关键标记 |
|---|---|---|---|
| `Bash` | `bash.py` | shell 命令，捕获 stdout/stderr/exit_code，超时可杀 | destructive, open_world |
| `Read` | `read.py` | 读文件带行号，支持 offset/limit | read_only |
| `Edit` | `edit.py` | 精确字符串替换，唯一性校验，产出 unified diff | destructive |
| `Write` | `write.py` | 新建/覆盖文件 | destructive |
| `Glob` | `glob_tool.py` | 模式匹配，按 mtime 新→旧 | read_only |
| `Grep` | `grep_tool.py` | 正则内容搜索（纯 Python re，无 ripgrep 依赖） | read_only |
| `memory_search` | `memory_search.py` | 模型侧 L2 查询，走共享 `MemorySearchEngine` | read_only |
| `memory_write` | `memory_write.py` | L2 规则唯一写入口：create/refine/supersede + 查重门禁 | destructive |
| `TodoWrite` | `todo_write.py` | 全量覆盖式会话 todo 列表，磁盘持久化 | destructive |
| `tool_search` | `tool_search.py` | 揭示 deferred 工具完整 schema | read_only |
| `skill_load` | `skill_load.py` | 按名取 bundled skill 全文（两阶段加载的第二阶段） | read_only |
| `EnterPlanMode` | `enter_plan_mode.py` | 写 `SessionConfig.metadata["mode"]="plan"`（enforcement 见下） | read_only |
| `EnterWorktree` | `enter_worktree.py` | `git worktree add` 并把路径写进 session metadata | destructive |
| `cron_create` | `cron.py` | 调度定时 prompt（注入 `SchedulerService`） | defer |
| `cron_list` | `cron.py` | 列出定时任务 | read_only, defer |
| `cron_delete` | `cron.py` | 按 ID 删除定时任务 | defer |
| `web_search` | `web_search.py` | 联网搜索（native/brave/duckduckgo） | read_only, open_world, defer |
| `web_fetch` | `web_fetch.py` | 抓 URL 抽正文（bs4 优先，正则回退） | read_only, open_world, defer |

辅助文件（非工具）：`_path.py`（相对 cwd 路径解析）、`_reindex.py`（写后 markdown 增量 reindex）、`_todo_store.py`（`TodoStore` 磁盘持久化）。

**memory_write（L2 规则写门禁）** 详见 [memory_rules_gate](memory_rules_gate.md)：L2/rules/*.md 的唯一写入口，create 强制 token 重叠 ≥0.6 查重，supersede 翻旧条目 `status: deprecated` + `superseded_by` 不删文件，deprecated 从搜索过滤。

## 关键工具行为

**TodoWrite 全量覆盖 + TodoStore 磁盘持久化**（`todo_write.py` / `_todo_store.py`）

- 语义同 CC 的 TodoWrite：每次调用**整体覆盖**会话的 todo 列表，模型每次都必须发全量快照（无增量/patch）。
- item 三字段：`content`（祈使句）、`active_form`（现在进行时）、`status ∈ {pending, in_progress, completed}`，validate 逐项校验。
- 持久化走构造注入的 `TodoStore`：每会话一个 JSON 文件 `~/.xana/sessions/{session_id}.todos.json`，tmp 写 + `os.replace` 原子替换；REPL 重启后列表仍在。`session_id` 做路径安全校验（拒 `/`、`\`、`.`、`..`）。

**EnterPlanMode 写 metadata，enforcement 已接线**（`enter_plan_mode.py` / chat_agent.py:160-164 / default.py:304-312）

- 行为：把 `SessionConfig.metadata["mode"]="plan"` 写盘（有 `SessionConfigStore` 时），返回 `metadata={"mode": "plan"}`。
- enforcement 链路（阶段一落地）：ChatAgent 构造 `HarnessContext` 时读 session config——`metadata["mode"]=="plan"` → `PermissionContext(mode="plan")`；harness 工具分发循环在 `validate` 之后检查：`permission.mode == "plan"` 且工具 `spec().read_only` 为假 → 直接 deny（"plan 模式：只允许只读工具"），不执行工具。read_only 工具（Read/Glob/Grep/memory_search 等）不受影响。

**tool_search 的 revealed_tools 闭环**见 [tools_spec](tools_spec.md) 的 defer 一节。

## Inputs

- `ToolInvocation`（tool_name, input, call_id, session_id, run_id?）
- `ToolContext`（session_id, agent_name, cwd, permission, run_id?, metadata）

## Outputs

- `ToolResult`（ok, content, structured_content?, error?, failure_kind?, rendered, artifacts, diffs, stdout/stderr, exit_code, truncated, retryable, metadata）
- 取消时的 synthetic warning（经 `ToolResult.content` 注入历史）
