---
title: agent adapter commands
type: doc
status: active
---

# agent adapter commands

## 上级模块

- [agent_adapter](agent_adapter.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agent_adapter/contracts.py) — `CommandSpec` / `CommandResult` / `CommandVisibility` / `TaskPanelRow`
- [interfaces.py](../src/xana/agent_adapter/interfaces.py) — `CommandRouter` ABC
- [commands.py](../src/xana/agent_adapter/commands.py) — `DefaultCommandRouter`

## 职责

斜杠命令的列表、路由与执行。命令分三类：改持久会话配置的（走 `SessionConfigStore`）、改易失状态的（直接操作 `ActiveContextStore` / gateway）、自包含的（`/help`）。

## 路由协议

`CommandRouter.route_command(inbound) -> CommandResult`。gateway 对每个入站消息先问 command router（见 [agent_adapter_gateway](agent_adapter_gateway.md)），消费规则：

- 非 `/` 开头 → `ok=False, payload={"pass_through": True}`，gateway 把消息当普通用户文本转发给 agent。
- 未注册命令 → `pass_through=False, unknown_command=True`，gateway 渲染错误并结束。
- 其余按 handler 执行；`payload.silent=True` 时不渲染卡片（如 `/compact_cancel`）；`payload.triggers_turn=True` 时 gateway 继续走普通 agent turn（`/compact`）；`payload.requires_detach=True` 时渲染前先 detach 当前前台 run（`/attach`）。

## DefaultCommandRouter

15 个公开命令（`list_commands()` 返回，供 `/help` 渲染）：`/help`（`/h` 同）、`/clear`、`/compact`、`/model`、`/effort`、`/streaming`、`/thinking`、`/async`、`/attach`、`/cancel`、`/project`、`/search_provider`、`/view`、`/resume`、`/tasks`；另有 1 个隐藏命令 `/compact_cancel <run_id>`（卡片取消按钮触发，不进 `/help`）。

要点：

- `/help`：输出命令清单；payload 带 send 型按钮（点击即发命令），TG 侧设 `linkify_commands` 隐去按钮。
- `/clear`：不删旧 session 数据，而是 rotate session——调 adapter 的 `rotate_session(thread_id)`（`BindingStore.rotate` 生成新 sid），同一条外部 thread 的下一条入站解析到新 sid，旧上下文/后台 run 保留在旧 sid 下。
- `/compact`：compact-as-PH 流程。先经 gateway detach 所有 in-flight run；有 in-flight 就复用其 PH 卡 run_id，否则自建 `compactcmd-...` 卡。经 `ContextSDK.append_compact_placeholder` 占位（拿 SDK compact 锁，防与并发写回竞争）→ `generate_summary` 流式压缩（进度经 `progress_sink` 发 `compact_progress`，带取消按钮）→ `replace_placeholder` 写回 `CompactSummary` → `compact_finalized` note_progress 落卡。结果恒 `silent=True`（卡片已由 note_progress 定态）。取消/失败走 `cancel_compact_placeholder`，不留下running PH。
- `/model` `/effort` `/streaming` `/thinking` `/async` `/search_provider` `/view` `/project`：无参列当前值+可点按钮，有参经 `_persist_with` 写 `SessionConfigStore`（`dataclasses.replace`，只改给定字段，`prompt_profile` / `metadata` 原样保留）。`/view detailed|grouped|summary|default` 写 `SessionConfig.view_mode`（default = 清空，回到渠道默认）。`/async off` 时有 running Placeholder 则拒绝。
- `/attach`：列出 session 内 running Placeholder 作为 `payload.attach_choices`，`requires_detach=True`；用户点按钮后由 adapter 调 `gateway.attach(sid, run_id)` 切前台。
- `/cancel`：有参取消指定 run（`gateway.cancel`）；无参列可取消菜单。
- `/resume`：无参列 `run_store.list_unfinished()` 中本 session 的可恢复 run；有参走 `gateway.resume`。
- `/tasks [session_id]`：任务面板。数据面 = `gateway.list_inflight()`（`_inflight` 活体 + RunStore 持久状态合并，见 [agent_adapter_gateway](agent_adapter_gateway.md)）；文本渲染 `run_id · 状态 · 时长 · 标签`（时长取 `metadata["started_at"]/["finished_at"]`，无 finished_at 的按当前时间算在途时长；detached 行标 `[后台]`）。cli daemon 渲染文本即可；feishu/tg 卡片尚未接入（后续接同一数据面）。

## 不变量

- 命令执行不直接发协议消息，只返回 `CommandResult`；渲染与卡片生命周期归 gateway/adapter。
- 命令进度复用同一张卡：`command_run_id` 由 gateway 预建卡时经 metadata 传入（`/compact` 除外，它自行决定 run_id）。
