---
title: 定时任务:独立执行、无人值守、按需通知
status: active
date: 2026-10-10
---

# 定时任务:独立执行、无人值守、按需通知

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

## 4. 指定 agent、无人值守与按需通知(2026-10-10 追加)

### 4.1 问题

- 到点总由会话绑定的 agent(缺省 chat)接手,想跑某个专门的 agent / 工作流只能靠 chat 再 `agent_run` 委派:多一次模型调用,也依赖 chat 照做。
- 有些任务应当后台静默运行(数据更新、定期整理),有些需要在关键时刻通知用户或请用户决策(监控、需要人拍板的流程);现在每次都把结果插回聊天,子会话也是交互模式。

### 4.2 参考

- Claude Code PushNotification:"用户不需要的通知会越积越烦,宁可不发。日常进度、几秒前刚问过的事、很快完成的小任务都不通知;只在用户可能已经走开、又有值得回来看的东西时通知,或用户明确要求时。"
- Kimi Code notify-user:进度更新只告知,不能用来提问或要决策;子 agent 的问题写进交给上级的结果里,由上级去问用户。
- Claude Code routines:无人值守运行,开头声明没人在实时看(已在 §3 采用)。

### 4.3 设计

1. **`cron_create` 新增两个可选参数**:
   - `agent`:到点直接在独立子会话里跑这个 agent(须是已注册的 agent,建任务时校验);不填 = 会话绑定的 agent(缺省 chat)。
   - `notify`:`report`(缺省)每次跑完把结果插回聊天;`silent` 后台静默,只在失败、需要用户决策或任务主动通知时才发消息。
   - 当初讨论的 `on_need` 与 `silent` 合并为 `silent`:二者只差"能不能主动通知",静默任务遇到要紧的事也应当能通知,没有必要单设一档。
2. **无人值守**:定时子会话执行前先把会话配置的提示词开关写为 `unattended`(不开目标模式);开关在会话内固定,之后用 `agent_run` 续跑时同样是无人值守。
3. **`notify_user` 工具**(工具组 `schedule`,只在无人值守会话中可见,同 `report_goal` 只在目标模式出现的做法;描述照 CC 的克制写法:宁可不发):
   - `kind="info"`:立即把一条通知插进该聊天的当前会话(主 agent 转告用户),任务继续。每个子会话最多 3 条,防止循环任务刷屏。
   - `kind="decision"`:写清问题与选项(`options`)。工具记下问题,并让模型结束本次执行、在最后一条消息里写清做到哪里、在等什么决定。
   - 不在定时任务里的无人值守会话调用它会失败并说明原因。
4. **决策之后继续**:本次执行结束后,调度器插入的通知状态为"等待你的决策",带上问题、选项和子会话编号,并说明"用户回复后,用 agent_run(resume=子会话编号) 把答复交给它继续"。`agent_run` 的 resume 接受本会话派出的定时子会话(`{当前会话}-cron-{任务}-{随机}`,agent 名取子会话记录的 `current_agent`)。续跑的结果经 `agent_run` 直接回到主 agent;续跑中再次请求决策时,问题写进最后一条消息,由主 agent 转告。
5. **何时插入通知**:

   | 情况 | report | silent |
   |---|---|---|
   | 正常完成 | 插入结果 | 不插入 |
   | 失败 / 取消 | 插入 | 插入 |
   | 请求决策 | 插入(等待决策) | 插入(等待决策) |
   | 运行中 `notify_user(info)` | 立即插入 | 立即插入 |

6. **查看静默任务**:每次执行后在任务记录里写 `last_run = {at, status, transcript}`;`cron_list` 列出 agent、通知方式和最近一次执行(时间、状态、记录路径)。一次性任务触发后不删除,记 `done_at` 并保留 7 天(不再触发),期间 `cron_list` 显示为"已执行"并带执行结果,过期由 `pop_due` 清掉。
7. **组合**:`ScheduledTaskDispatcher` 改由 `boot()` 构造(唯一组合点,`BootResult.scheduler_dispatch`),serve 只负责启动 `SchedulerRunner`;`NotifyUserTool` 构造注入 dispatcher。

## 5. 影响

- 已有任务没有 `route`,按建任务的会话送达(行为与旧版相同,只是改为独立执行)。线上两个项目当前都没有定时任务。
- `session-and-card-state.md` 的会话规格不变:子会话是独立会话,插回的通知是一条普通入站消息。
- 删除 `cli/serve.py` 的 `_make_scheduler_dispatch`,dispatch 签名改为 `dispatch(job)`。

## 6. 验证

- `tests/test_scheduler.py`:路由记录、`/clear` 后送到新会话、子会话参数、通知内容、bind_session、失败通知、长任务不阻塞轮询。
- `tests/test_agent_composition.py`:真实 boot 下子会话看不到主会话历史,带定时说明、不带子 agent 说明。
- 元 agent cron 走真实 `ScheduledTaskDispatcher`(`tests/test_meta_agent.py`)。
- 追加:`agent` 参数校验与执行;silent 完成不插入、失败与决策插入;子会话为无人值守、`notify_user` 只在无人值守会话可见;info 立即送达与条数上限;决策后 `agent_run` resume 定时子会话;`last_run` 记录与 `cron_list` 展示。
