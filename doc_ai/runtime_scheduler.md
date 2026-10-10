---
title: runtime scheduler
type: doc
status: active
---

# runtime scheduler

## 上级模块

- [runtime](runtime.md) — 模块设计

## 代码锚点

- [scheduler.py](../src/xana/runtime/scheduler.py) — `FileSchedulerService`、`SchedulerRunner`、`ScheduledTaskDispatcher`
- [capabilities.py](../src/xana/extension_api/capabilities.py) — `SchedulerService`
- [cron.py](../src/xana/tools/builtin/cron.py) — `CronCreateTool` / `CronListTool` / `CronDeleteTool`
- [schedule.py](../src/xana/skills/bundled/schedule.py) — `ScheduleSkill`（bundled skill）
- [notify_user.py](../src/xana/tools/builtin/notify_user.py) — `NotifyUserTool`
- [boot.py](../src/xana/runtime/boot.py) — `ScheduledTaskDispatcher` 组装；[serve.py](../src/xana/cli/serve.py) — Runner 启动

## 职责

把"定时任务"做成一个闭环：模型经 cron 工具建任务 → 文件持久化 → serve 内轮询到点 → 在**无人值守的独立子会话**里执行（只带任务内容，不带聊天历史）→ 按通知方式把结果作为一条 `<task-notification>` 插进该聊天的**当前**会话，由主 agent 转告用户，用户可基于结果继续追问。照 Claude Code 后台子 agent；设计见 [scheduled-task-design](../docs/design/scheduled-task-design.md)。

## 接口

**`SchedulerService`**（extension_api/capabilities.py）— Extension API 的运行期服务 ABC，`add(cron, prompt, recurring) / remove(task_id) / list_all()`，构造注入给 Extension。

实现是 **`FileSchedulerService`**（runtime/scheduler.py）：

- 持久化 `~/.xana/projects/<id>/scheduled_tasks.json`（boot 注入路径），每条任务 `{id, cron, prompt, recurring, session_id, route, agent, notify, created_at, next_fire, last_run}`；`next_fire` 由 croniter 按本地时区算出。文件损坏（半写/非法 JSON）记警告、从空重建，不杀死进程。
- `add(..., agent=None, notify="report")`：`agent` = 到点运行的 agent（None = 会话绑定的 agent，缺省 chat）；`notify` ∈ `report`（每次插回结果）/ `silent`（只在失败、请求决策或运行中 `notify_user` 时插入）。
- `route`：构造注入 `route_of=SessionResolver.route_of`，`add()` 记下创建会话所在的外部会话 `[adapter_id, thread_id]`（反查不到为 null）。公有可变属性 `session_id` 由 `CronCreateTool.execute` 在 `add` 前用 `ToolContext.session_id` 覆写（同一事件循环内串行）。
- `pop_due(now_epoch)`：到点任务一次性删除、循环任务滚动 `next_fire`；`record_run(task_id, at, status, transcript)` 写 `last_run`（一次性任务已删除时跳过）。

## SchedulerRunner

serve 内的轮询器：`start()` 每 `poll_interval_s`（默认 30s）`tick()` → `pop_due()` → 每个到点任务放进各自的后台任务执行 `dispatch(job)`（长任务不阻塞其他任务与下一次轮询；`drain()` 等全部结束）。dispatch 异常只记日志。serve 用 `SchedulerRunner(scheduler=boot_result.scheduler, dispatch=boot_result.scheduler_dispatch)` 启动。

## ScheduledTaskDispatcher（到点执行）

boot 构造（唯一组合点，`BootResult.scheduler_dispatch`），依赖 gateway、`AgentRunner`、`FileSchedulerService`、`SessionConfigStore`、`SessionResolver`、`MetaEvolutionService`：

1. 元 agent 的 marker 任务 → `meta_service.fire()`（见 [evolution_module](evolution_module.md)），不走以下流程。
2. 当前会话 = `resolver.current(*route)`（没有路由时用建任务的会话），`/clear` 换了会话也送到新会话。agent = 任务的 `agent` 或会话绑定的 agent。
3. 子会话 `{当前会话}-cron-{任务 id}-{agent}-{随机}`：执行前把它的提示词开关写为 `unattended`（不开目标模式，会话内固定，续跑也保持），`AgentRunner.run(..., preamble=SCHEDULED_TASK_PREAMBLE)`（改写自 CC routines：定时器自动发起、没人在实时看、不在回复里提问，必须由用户拍板时用 `notify_user`）。子会话起不来按失败处理。
4. 结束后 `record_run`，再按情况插入通知：请求过决策 → 决策通知（问题、选项、进展、子会话编号，并让主 agent 在用户答复后 `agent_run(resume=子会话)`）；失败 / 取消 → 结果通知；正常完成且 `notify=report` → 结果通知（任务、结果、完整过程路径 `sessions/runs/<run_id>.jsonl`）；`silent` 正常完成不插入。
5. 送达：先 `adapter.bind_session(route)`（serve 重启后卡片也能发到该聊天），再以 `InboundMessage(metadata={"source": "scheduler", "chat_id": ...})` 走 `gateway.submit`。
6. 供 `notify_user` 调用：`notify(child, message)` 立即把告知插进当前会话（会话由子会话编号解析，续跑中同样可用；每个子会话最多 `MAX_INFO_NOTICES=3` 条；非定时会话返回原因）；`request_decision(child, question, options)` 只登记调度器正在执行的子会话，执行结束后随决策通知发出。

## cron 工具、notify_user 与 schedule skill

- **cron_create / cron_list / cron_delete**（tools/builtin/cron.py）：`defer=True`，permission 恒 allow。create 校验 cron 合法性、`notify` 取值，`agent` 须在活的 agent 表中（构造注入 `agents`）；list 显示 agent、通知方式与最近一次执行（时间、状态、记录路径）。
- **notify_user**（tools/builtin/notify_user.py，工具组 schedule）：只在无人值守会话中可见（harness 按 `PromptProfile.mode` 过滤）；`kind=info` 立即告知、任务继续，`kind=decision` 写清问题与选项后结束本次执行（不在调度器执行中时，让模型把问题写进最后一条消息由调用方转告）。描述照 CC PushNotification：宁可不发。
- **agent_run** 的 `resume` 接受本会话派出的定时子会话（见 [agents_subagents](agents_subagents.md)）。
- **ScheduleSkill**（skills/bundled/schedule.py）：弄清意图 → 定时间并用人话复述 → 选 agent 与通知方式 → cron_create → 告知任务 ID；查看/修改/删除对应 cron_list / delete+create / cron_delete。

## 不变量

- 任务的唯一权威是 scheduled_tasks.json + `FileSchedulerService`；serve 重启后从文件恢复。
- 任务执行走 `AgentRunner`（独立子会话，与 agent_run 同一实现）；插回主会话的只有一条普通入站通知，会话规格（session-and-card-state.md）不变。
- 失败与待决策总会通知，不因 `silent` 而静默消失。

## 验收要求

- 输入/输出与 [runtime](./runtime.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
