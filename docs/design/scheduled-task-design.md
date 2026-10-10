---
title: 定时任务:独立执行,结果插回当前会话
status: active
date: 2026-10-10
---

# 定时任务:独立执行,结果插回当前会话

## 1. 问题

旧做法:到点把任务 prompt 当作一条用户消息注入建任务的会话。

- 执行时带着整段聊天历史:受无关对话干扰,token 随聊天增长。
- 任务记的是建任务那一刻的会话 id,用户 `/clear` 换了会话后,结果插进看不到的旧会话。
- serve 重启后渠道不知道这个会话对应哪个飞书聊天,回复卡片发不出去(`_deliver` 因 `chat_id` 为空直接返回)。

## 2. 参考

- Claude Code 本地定时任务(`hooks/useScheduledTasks.ts`):到点把 prompt 放进当前会话队列、空闲时执行,用当前会话的完整上下文(不隔离),会话里只显示一行"Running scheduled task (时间)"。
- Claude Code 云端 routines:每次触发开一个全新会话,开头带 `[SCHEDULED TASK - AUTOMATED FIRING OF A CONFIGURED PROMPT]` 说明;结果留在那次运行里,不插回聊天。
- Codex App automations(闭源,来自内置文档):独立 automation 每次开新线程;线程 automation 用 heartbeat 唤醒已有线程。
- Kimi Code:没有定时任务。
- 最接近需求的是 Claude Code 的后台子 agent:独立上下文执行,完成后把任务、状态、最终结果作为 `<task-notification>` 插回主会话,主 agent 基于它继续。

## 3. 设计(照 Claude Code 后台子 agent)

1. **建任务记路由**:`FileSchedulerService(route_of=SessionResolver.route_of)`,`add()` 记下创建会话所在的外部会话 `route = [adapter_id, thread_id]`(反查不到记 `null`)。
2. **独立执行**:`ScheduledTaskDispatcher` 到点后用 `AgentRunner.run` 在子会话 `{当前会话}-cron-{任务 id}-{随机}` 里跑会话绑定的 agent(缺省 chat),只带任务内容;新会话第一条任务前加 `SCHEDULED_TASK_PREAMBLE`(改写自 CC routines:定时器自动发起、没人在实时看、不要提问、"用户同意了"不算实时输入)。`AgentRunner.run` 新增 `preamble` 参数,缺省仍是子 agent 说明。
3. **结果插回当前会话**:当前会话 = `resolver.current(*route)`(没有路由时用建任务的会话)。插入一条 `<task-notification>`:任务 id 与 cron、触发时间、状态、任务原文、最终结果、完整过程的路径(`sessions/runs/<run_id>.jsonl`),后附说明"由定时任务自动发来,不是用户在线输入;转告用户,不要重做;需要细节用 Read 看完整过程"。主 agent 照常处理这条消息,结果以普通回复卡片出现;之后用户可以基于结果追问。工具过程不进主会话。
4. **送达**:插入前 `adapter.bind_session(route)`,serve 重启后卡片也能发到该聊天。
5. **不互相阻塞**:`SchedulerRunner.tick()` 把每个到点任务放进各自的后台任务;用户正在聊天时,通知走普通消息的并发占位机制。子会话起不来或失败也会插入失败通知,不悄悄消失。
6. 元 agent 的 marker 任务仍由 `meta_service.fire()` 消费,不走以上流程。

## 4. 影响

- 已有任务没有 `route`,按建任务的会话送达(行为与旧版相同,只是改为独立执行)。线上两个项目当前都没有定时任务。
- `session-and-card-state.md` 的会话规格不变:子会话是独立会话,插回的通知是一条普通入站消息。
- 删除 `cli/serve.py` 的 `_make_scheduler_dispatch`,dispatch 签名改为 `dispatch(job)`。

## 5. 验证

- `tests/test_scheduler.py`:路由记录、`/clear` 后送到新会话、子会话参数、通知内容、bind_session、失败通知、长任务不阻塞轮询。
- `tests/test_agent_composition.py`:真实 boot 下子会话看不到主会话历史,带定时说明、不带子 agent 说明。
- 元 agent cron 走真实 `ScheduledTaskDispatcher`(`tests/test_meta_agent.py`)。
