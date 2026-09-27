---
title: runtime scheduler
type: doc
status: active
---

# runtime scheduler

## 上级模块

- [runtime](runtime.md) — 模块设计

## 代码锚点

- [scheduler.py](../src/xana/runtime/scheduler.py) — `FileSchedulerService`、`SchedulerRunner`
- [capabilities.py](../src/xana/extension_api/capabilities.py) — `SchedulerService`
- [cron.py](../src/xana/tools/builtin/cron.py) — `CronCreateTool` / `CronListTool` / `CronDeleteTool`
- [schedule.py](../src/xana/skills/bundled/schedule.py) — `ScheduleSkill`（bundled skill）
- [serve.py](../src/xana/cli/serve.py) — `_make_scheduler_dispatch`、Runner 启动

## 职责

把"定时任务"做成一个闭环：模型经 cron 工具建任务 → 文件持久化 → serve 内轮询到点 → prompt 注回对应 session。调度器只做外壳（存取、触发、路由），不拥有任务内容——prompt 注入后走普通 inbound 管线，行为由 agent/harness 决定。

## 接口

**`SchedulerService`**（extension_api/capabilities.py:15）— Extension API 的运行期服务 ABC，`add(cron, prompt, recurring) / remove(task_id) / list_all()`，构造注入给 Extension。这是调度的唯一接口。

实现 `SchedulerService` 的是 **`FileSchedulerService`**（runtime/scheduler.py:25）：

- 持久化 `~/.xana/scheduled_tasks.json`（boot 注入实际路径），每条任务 `{id, cron, prompt, recurring, session_id, created_at, next_fire}`；`next_fire` 由 croniter 按本地时区算出。
- 文件损坏（半写/非法 JSON）不杀死进程：记警告、丢弃并从空重建。
- 公有可变属性 `session_id`：ABC 的 `add()` 不收 session_id，而任务必须记录创建它的会话——`CronCreateTool.execute` 在 `add` 前用 `ToolContext.session_id` 覆写。Runner 串行消费、工具执行同事件循环串行，无并发风险。
- `pop_due(now_epoch)`：到点任务一次性删除、循环任务滚动 `next_fire` 后回库（返回的 due 条目保留触发时刻的 `next_fire`）。Runner 专用。

## SchedulerRunner（runtime/scheduler.py:82）

serve 内的轮询器：`start()` 起 asyncio task，每 `poll_interval_s`（默认 30s）调 `tick()` → `pop_due()` → 对每个到点任务 `dispatch(session_id=..., text=...)`。dispatch 异常只记日志，单个失败任务或 `pop_due` 意外都不能杀死轮询。serve.py 里 `SchedulerRunner(scheduler=boot_result.scheduler, dispatch=_make_scheduler_dispatch(gateway), ...)` 启动。

dispatch 回调（serve.py `_make_scheduler_dispatch`）把 prompt 包装成 `InboundMessage(adapter_id 按 session_id 前缀推断：tg-→telegram、feishu-→feishu，无匹配退回首个 adapter, metadata={"source": "scheduler"})` 走 `gateway.submit`——与 adapter 入站泵同一条路径。

## cron 工具与 schedule skill 的联动

- **cron_create / cron_list / cron_delete**（tools/builtin/cron.py）：三个工具都 `defer=True`（默认只见名字，经 tool_search 揭示后可用）；permission 恒 allow。create 校验 cron 合法性（`croniter.is_valid`，非法返回 `failure_kind="validation"`）。
- **ScheduleSkill**（skills/bundled/schedule.py）：bundled skill，指导模型走完"弄清意图 → 定时间 → 用人话复述确认 → cron_create → 告知任务 ID"的交互流程；查看/修改/删除分别对应 cron_list / delete+create / cron_delete。任务持久化在 `~/.xana/scheduled_tasks.json`，serve 重启后仍有效。

## 不变量

- 任务的唯一权威是 scheduled_tasks.json + `FileSchedulerService`；serve 重启后从文件恢复。
- 调度触发只产生 inbound，不直接调 harness——执行语义与普通消息一致。

## 验收要求

- 输入/输出与 [runtime](./runtime.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
