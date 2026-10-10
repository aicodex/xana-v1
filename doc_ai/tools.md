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
| `report_goal` | `report_goal.py` | 目标模式下声明目标终态（done / blocked / failed + 原因）；只在 `goal_mode` 会话的 schema 中出现 | read_only |
| `skill_load` | `skill_load.py` | 按名取 bundled skill 全文（两阶段加载的第二阶段）；不在 `ToolContext.visible_skills` 内的技能按不存在处理（`not_found`，只列可见技能） | read_only |
| `EnterPlanMode` | `enter_plan_mode.py` | 写 `SessionConfig.metadata["mode"]="plan"`（enforcement 见下） | read_only |
| `EnterWorktree` | `enter_worktree.py` | `git worktree add` 并把路径写进 session metadata | destructive |
| `cron_create` | `cron.py` | 调度定时任务：`agent` 指定运行的 agent，`notify` = report / silent；到点在无人值守的独立子会话执行，结果按通知方式插回当前聊天（见 [runtime_scheduler](runtime_scheduler.md)） | defer |
| `cron_list` | `cron.py` | 列出定时任务（agent、通知方式、最近一次执行的时间 / 状态 / 记录路径） | read_only, defer |
| `notify_user` | `notify_user.py` | 只在无人值守会话可见：`info` 立即告知用户（每次任务 ≤3 条），`decision` 请用户决策并结束本次执行，答复后经 `agent_run` resume 继续 | open_world |
| `cron_delete` | `cron.py` | 按 ID 删除定时任务 | defer |
| `web_search` | `xana_search.py` | 配置了 xana-search 服务（`system.json` `web_search.xana_search_url/token`）时注册：1–4 条查询并行，只返回标题/链接/日期/搜索引擎摘要 | read_only, open_world, parallel |
| `web_open_url` | `xana_search.py` | 同上：打开 1–10 个网页读全文，给 objective 时由小模型提炼要点（原文引用逐字核对）；执行中经 `report_progress` 上报阅读进度 | read_only, open_world, parallel |
| `background_research` | `xana_search.py` | 同上：服务端后台研究任务（本地 27B 规划 + 4B 阅读，不占主模型），提交返回 job_id，`wait_s` 长轮询（期间上报进度） | read_only, open_world, parallel |
| `deep_research` | `lead_research.py` | 配置了 xana-search 时注册：主模型当研究负责人拆子课题（effort low/medium/high = 3/4/6）→ 经 `AgentRunner` 并行跑内置 `researcher` 子 agent → 汇总成带出处的报告；实时进度，子研究员原始发现写到工作区 `research/<日期>/`（设计见 [lead-research-design](../docs/design/lead-research-design.md)） | read_only, open_world, parallel |
| `agent_run` | `agent_run.py` | 把子任务委派给另一个 agent，在独立子会话里同步跑完并取回最终回答；可 `resume` 续接（也接受本会话的定时子会话），子 agent 工具 = 自身 ∩ 调用方上限（见 [agents_subagents](agents_subagents.md)） | open_world, parallel |
| `agent_define` | `agent_define.py` | `catalog` 看工具组目录与已有 agent；`create` 校验后按 `scope` 写 `<project_root>/.xana/agents/<name>/AGENT.md`（`project`，缺省）或 `~/.xana/agents/<name>/AGENT.md`（`user`），强制刷新注册表后立即可用（不能提权、不能覆盖内置）；`delete` 删除文件 agent 并立即生效（内置不能删），见 [agents](agents.md) | — |
| `project_manage` | `project_manage.py` | **只在 default 项目注册**（工具组 `projects`）：`list` / `create`（名字、目录、`agent_defaults`、`instructions`：写进项目根目录 `AGENTS.md`，已有不覆盖）/ `configure`（改 `agent_defaults`，未知工具 / 组 / 技能名报错）/ `bind_feishu`（飞书扫码创建应用，立即返回授权链接，后台轮询，确认后写入该项目 channels 并把确认人加入配对白名单）/ `feishu_status`（等待绑定结果，最多 2 分钟，上报进度）/ `start` / `stop`（不能停 default）；逻辑在 `runtime/projects.py`，设计见 [project-design](../docs/design/project-design.md) §7 | open_world |
| `web_search`（旧） | `web_search.py` | 未配置 xana-search 时的回退：联网搜索（native/brave/duckduckgo） | read_only, open_world, defer |
| `web_fetch`（旧） | `web_fetch.py` | 未配置 xana-search 时的回退：抓 URL 抽正文（bs4 优先，正则回退） | read_only, open_world, defer |

辅助文件（非工具）：`_path.py`（相对 cwd 路径解析）、`_reindex.py`（写后 markdown 增量 reindex）、`_todo_store.py`（`TodoStore` 磁盘持久化）。

**memory_write（L2 规则写门禁）** 详见 [memory_rules_gate](memory_rules_gate.md)：L2/rules/*.md 的唯一写入口，create 强制 token 重叠 ≥0.6 查重，supersede 翻旧条目 `status: deprecated` + `superseded_by` 不删文件，deprecated 从搜索过滤。

## 关键工具行为

**TodoWrite 全量覆盖 + TodoStore 磁盘持久化**（`todo_write.py` / `_todo_store.py`）

- 语义同 CC 的 TodoWrite：每次调用**整体覆盖**会话的 todo 列表，模型每次都必须发全量快照（无增量/patch）。
- item 三字段：`content`（祈使句）、`active_form`（现在进行时）、`status ∈ {pending, in_progress, completed}`，validate 逐项校验。
- 持久化走构造注入的 `TodoStore`：每会话一个 JSON 文件 `~/.xana/projects/<id>/sessions/{session_id}.todos.json`，tmp 写 + `os.replace` 原子替换；REPL 重启后列表仍在。`session_id` 做路径安全校验（拒 `/`、`\`、`.`、`..`）。

**EnterPlanMode 写 metadata，enforcement 已接线**（`enter_plan_mode.py` / chat_agent.py:160-164 / default.py:304-312）

- 行为：把 `SessionConfig.metadata["mode"]="plan"` 写盘（有 `SessionConfigStore` 时），返回 `metadata={"mode": "plan"}`。
- enforcement 链路（阶段一落地）：ChatAgent 构造 `HarnessContext` 时读 session config——`metadata["mode"]=="plan"` → `PermissionContext(mode="plan")`；harness 工具分发循环在 `validate` 之后检查：`permission.mode == "plan"` 且工具 `spec().read_only` 为假 → 直接 deny（"plan 模式：只允许只读工具"），不执行工具。read_only 工具（Read/Glob/Grep/memory_search 等）不受影响。

**tool_search 的 revealed_tools 闭环**见 [tools_spec](tools_spec.md) 的 defer 一节。

## Inputs

- `ToolInvocation`（tool_name, input, call_id, session_id, run_id?）
- `ToolContext`（session_id, agent_name, cwd, permission, run_id?, metadata, report_progress?, available_tools?, visible_skills?）

## Outputs

- `ToolResult`（ok, content, structured_content?, error?, failure_kind?, rendered, artifacts, diffs, stdout/stderr, exit_code, truncated, retryable, metadata）
- 取消时的 synthetic warning（经 `ToolResult.content` 注入历史）
