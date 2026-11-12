---
title: Codex Claude Code Kimi Code 后台任务机制源码调研
type: experience
status: reference
date: 2026-11-09
---

# Codex Claude Code Kimi Code 后台任务机制源码调研

## 目标与结论

Xana 需要支持两种使用方式：主 agent 可以等待子 agent、评测或长任务的结果，也可以先完成本轮回答，待后台任务结束后再处理结果。用户插话应当及时交还对话控制，已经交出的任务继续执行。

建议以 Kimi 的任务完成通知与主循环衔接为主要参照，结合 Claude Code 的通知优先级和 Codex 的唤醒准入。任务执行、等待观察、结果交付和会话唤醒应分别定义；继续使用每会话一个活动执行循环，不恢复旧对话后台模式、attach/detach 或上下文占位符。

本文保留实现前的源码调研与风险判断。后续已按此完善后台交付，当前契约见[后台任务](../doc_ai/harness_background_tasks.md)及[会话、插话与取消](../docs/design/session-and-card-state.md)；源码事实仍限定于下列审阅版本。

## 源码版本与适用范围

| 系统 | 审阅版本 | 范围 |
| --- | --- | --- |
| Codex | [8703d32bd0ea5cf26768859a3dd476df906ba053](https://github.com/openai/codex/commit/8703d32bd0ea5cf26768859a3dd476df906ba053)，2026-11-09 | 本地 `best_agent/codex` 已快进到该 main；重点为当前 V2 多 agent、邮箱和唤醒准入 |
| Kimi Code | [725194d7e127811ed754dde0e98c4788866d779f](https://github.com/MoonshotAI/kimi-code/commit/725194d7e127811ed754dde0e98c4788866d779f)，2026-11-09 | 本地 `best_agent/kimi-code` 已快进到该 main；重点为 agent-core-v2 的任务服务和主循环 |
| Claude Code | 本地 `best_agent/claude-code-2.1.88-src` 可读源码快照 | 结论限于该快照。本地 2.1.316 材料是从可执行文件提取的提示词，不能作为新版完整实现的证据 |

`best_agent` 位于 Xana 仓库的同级目录。Codex 和 Kimi 的下列远程链接固定到上述提交，避免后续 main 变化影响复核。测试证据来自阅读实现与断言，未运行三家的完整测试，不能据此宣称已完成端到端验证。

## 三家的执行机制

| 机制 | Codex 当前 V2 | Claude Code 本地快照 | Kimi Code |
| --- | --- | --- | --- |
| 等待 | `wait_agent` 等邮箱活动、用户插话或超时；V1 另有按目标订阅状态的等待实现 | Agent 工具可同步执行，也可后台执行；`TaskOutput(block=true)` 可以等待 | `WaitFor` 可以等指定任务或调用时正在运行的任一任务 |
| 提前结束等待 | 输入活动可以唤醒等待，等待本身不负责终止子 agent | 等待输出受当前调用的取消信号控制；显式后台 agent 有独立取消控制 | 区分 `steerSignal` 与真正取消；插话结束等待，后台任务继续运行 |
| 完成结果 | 路由到父 agent 邮箱，是否启动新 turn 另行判断 | 完成状态更新后，将 `task-notification` 排入会话队列 | 任务服务调用 `loop.notify`，忙时交给当前循环，空闲时可启动一轮 |
| 用户输入与通知 | 用户输入与 agent 邮件有不同类型和准入规则 | 用户输入默认 `next`，任务通知默认 `later` | 通知携带 `origin.kind=task`、任务 ID、状态和通知 ID |
| 防重复 | 邮箱准入、消费与执行分开；完成投递明确为 best effort | `notified` 标志避免同一进程重复排入通知 | 区分构建、排队与已消费状态，并结合上下文事件和重放状态判断交付 |
| 长输出 | 长命令可返回进程 ID，继续观察输出；不能据此推定空闲会话必然自动唤醒 | agent 转录和 shell 输出有文件路径，通知可携带摘要及路径 | 通知、输出预览与完整输出文件分离，支持后续按需读取 |

### Codex 将入邮箱和唤醒分开

`notify_parent_of_terminal_turn` 构造完成消息时设置 `trigger_turn=false`。`inter_agent_communication` 先入邮箱，再根据显式触发、`WakeOnAgentMessages` 或 durable sleep 判断是否请求启动。`reserve_pending_work_turn` 在准入锁下检查是否已有活动 turn，再决定是否预留新 turn。

该版本的 `wake_on_agent_messages` 为 `UnderDevelopment`，默认关闭。自动唤醒还检查被打断状态和会话模式。因此不能把 Codex 的“子 agent 完成通知”理解为“主回答结束后总会自动续答”，也不能把其邮箱描述为跨进程重启的持久消息队列。

源码：[完成路由][codex-completion]、[消息入口][codex-handlers]、[唤醒准入][codex-admission]、[功能默认值][codex-features]、[V2 等待][codex-wait]。`session/turn_input_tests.rs::agent_message_wake_reservation_preserves_existing_boundaries` 覆盖被打断、Plan 模式和继承子配置的边界。

### Claude Code 通过会话队列继续处理

`AgentTool` 的显式后台分支调用 `registerAsyncAgent` 时，不连接父回答的 abort controller，使后台 agent 可以在用户按 Esc 停止主回答后继续执行。停止后台任务是另一个明确动作；进程退出也有独立清理。

`runAsyncAgentLifecycle` 先更新任务完成状态，解除取结果的等待，再处理报告修饰和工作区清理，最后发送通知。这些附加工作可能阻塞，不能挡住任务状态进入终态。

`enqueueAgentNotification` 检查并设置 `notified`，通过 `enqueuePendingNotification` 排队。通知默认优先级低于用户输入。主会话空闲且没有阻塞界面时，`useQueueProcessor` 驱动 `processQueueIfReady` 处理；不同消息模式不会被混成一组。通知还标明是后台任务来源，不按普通用户要求解释。

本地源码入口：

- `tasks/LocalAgentTask/LocalAgentTask.tsx`：`registerAsyncAgent`、`enqueueAgentNotification`。
- `tools/AgentTool/AgentTool.tsx`：`shouldRunAsync` 分支及父取消信号的处理。
- `tools/AgentTool/agentToolUtils.ts`：`runAsyncAgentLifecycle`。
- `utils/messageQueueManager.ts`、`utils/queueProcessor.ts`、`hooks/useQueueProcessor.ts`：通知入队、优先级和空闲消费。
- `tools/TaskOutputTool/TaskOutputTool.tsx`、`tasks/LocalShellTask/LocalShellTask.tsx`：等待输出与 shell 完成通知。

这份快照的 `notified` 主要避免进程内重复通知，不能推导出“结果写入历史和确认送达具有跨重启事务保证”。

### Kimi 让任务通知进入统一主循环

任务进入终态后，任务服务构造带来源的通知，通过 `loop.notify` 交给主循环；设置 `turnScoped=false`，使该通知不只属于发起任务的那一轮。`LoopNotify` 还提供 `onConsume` 和 `onDrop`，区分消费和丢弃。

任务通知使用任务 ID、状态和固定通知 ID 生成去重键。任务服务跟踪排队与交付，结合 `ContextAppendMessage`、上下文变更及重放状态恢复判断。`WaitFor` 取回结果后会标记经等待交付，并撤掉对应待处理通知。它仍不是 Xana 可以直接照搬的“工具结果与历史写入原子事务”。

主 agent 的等待超时提示鼓励先做其他工作或向用户报告进度，完成后自动通知。临时子 agent 则被提醒：最终交班前必须取得其所需的后台结果，否则交班后的通知可能无人接收。这与主会话长期存在的生命周期不同。

源码：[任务服务][kimi-tasks]、[主循环通知][kimi-loop]、[等待工具][kimi-wait]。Bash 长进程也接入同一任务服务；这项能力不等于 Xana 当前 Bash 已支持后台执行。

## Kimi 的回归场景

[idle-notification-repro.test.ts][kimi-tests] 中的以下场景值得转为 Xana 验收用例：

| 场景 | 测试表达的行为 |
| --- | --- |
| IDLE | 主 agent 没有活动 turn 时，后台完成通知启动一轮，模型能看到任务通知 |
| BUSY | 主 agent 已有活动 turn 时，完成事件进入后续模型调用 |
| IDLE × N | 多个后台任务集中完成，通知进入主循环处理，模型上下文包含各任务身份 |
| RACE | 完成恰好发生在主 turn 结束之后，仍能启动处理通知的一轮 |
| stop | 停止后台子 agent 后，等子循环真正空闲，再公布 killed 状态与通知 |
| RESUME | 上次进程遗失的任务汇总为恢复提醒，不因恢复而自动发起一轮 |

这些测试不能证明任意时序下所有任务只消耗一次模型请求；应分别验证通知不遗漏、重复抑制和会话内不并发启动两个回答。

## 审阅时 Xana 的基础与缺口

现有 [BackgroundTasks](../src/xana/harness_sdk/background.py) 已支持等待返回、运行中注记、父 run 结束后通知、任务停止、硬超时和重启中断记录。[agent_run](../src/xana/tools/builtin/agent_run.py) 与评测共用该机制；[task_wait](../src/xana/tools/builtin/task_tools.py) 可被用户插话唤醒。现有机制应在原位完善。

| 缺口 | 当前代码依据 | 影响 |
| --- | --- | --- |
| 等待确认早于历史落盘 | `BackgroundTasks.wait` 在返回前设置 `delivered=True`，工具结果随后才由 harness 写入历史 | 中间取消或写盘失败，存在记录已确认而结果尚未进入主历史的窗口 |
| 自动通知缺少固定输入身份 | `_notify_session` 仅传会话 ID 和文本；`SessionDelivery.deliver` 未为该任务通知设置稳定 `input_id` | 仅靠进程内 `_notifying` 无法完成重启后的输入去重 |
| 通知来源进入历史时不完整 | `SessionDelivery` 带 `source`，但 `DefaultHarness._apply_inputs` 将其写成只带输入 ID 的普通 user 消息 | 主历史不能充分区分后台结果和真正用户输入 |
| 通知回调等待整个主回答 | `SessionDelivery.deliver` 排干 `gateway.submit` 才返回；`run_ended` 对完成记录逐个等待通知 | “已接收”与“已处理”耦合，多个结果的合并和重试更难控制 |
| 取消终态早于执行收尾 | `_with_hard_timeout` 发出 `task.cancel()` 后不等待内部协程结束；`GatewayAgentRunner` 取消路径请求子 run 停止后返回 | 登记表可能已宣布停止，子执行仍在清理或写入 |

代码入口：[结果投递](../src/xana/runtime/delivery.py)、[主循环](../src/xana/harness_sdk/default.py)、[子 agent 执行](../src/xana/agent_adapter/agent_runner.py)。上述为源码可见的风险窗口，尚未通过新增故障注入用例逐项复现。

Xana 的无人值守或子 run 当前还有最多 20 次的 `background_pending` 模型续跑提醒。实现时需检查它是否在依赖模型反复调用等待来维持存活；必要的等待应由事件驱动，避免为了保持运行反复消耗模型请求。

## 建议实现路径

1. **确定任务执行与等待的所有权。** 明确开始等待、交出后台任务、停止当前回答和停止后台任务的区别；公布停止终态前等待实际执行收尾。
2. **统一交付身份与确认时点。** 每个完成事件有稳定身份；查询结果不提前代替历史提交确认。运行中注记和空闲通知使用同一结果交付规则，减少重复实现。
3. **通过当前主循环消费通知。** 忙时在安全边界处理，空闲时经同一准入启动一轮；保留任务来源。通知入队的确认不必等模型整轮回答结束，用户输入优先于自动通知。
4. **完善持久恢复。** 恢复未消费的完成事件并去重；中断任务展示为中断，不自动重放可能产生副作用的工作。结果摘要和完整输出位置应可查。
5. **再完善工具体验。** 支持等待、非阻塞查进度、读结果和明确停止。Bash 长命令是否纳入本轮实现仍待确定，不把工具描述写成已经支持。

不增加第二套对话执行路径，不为旧 attach 或占位符保留兼容入口，也不借此引入账户预算账本。

## Xana 验收与行为边界

验收至少覆盖：立即完成、等待超时后完成、插话打断等待、主回答结束后完成、多个结果集中完成、收尾竞态、等待取结果与自动通知竞争、结果写盘失败、通知接收后确认前重启、停止等待不停止已交出任务，以及停止后台任务后真实执行已经退出。

同时验证已有预算和权限仍约束被唤醒的执行，恢复不会重跑副作用，旧 attach/detach 入口持续不可用。

沿用当前取消语义：已交出的后台任务可继续执行；父回答明确取消后，其结果保留到用户下一轮，不自动唤醒已停止的回答；停止后台任务使用明确的任务停止动作。当前重启恢复标记任务中断，不自动重放副作用。这些边界不能因参照其他系统而悄悄改变。

仍需明确的范围包括：用户 `/clear` 后旧任务结果的归属，以及本轮是否同时接入 Bash 长命令后台执行。如要改变现有取消和重启行为，需先说明具体变化并取得用户确认。

此前的[Codex 插话机制审阅](context_codex_steering.md)说明单循环与插话边界；本篇补充独立任务的等待、完成通知和空闲唤醒。

[codex-completion]: https://github.com/openai/codex/blob/8703d32bd0ea5cf26768859a3dd476df906ba053/codex-rs/core/src/agent/control/completion.rs
[codex-handlers]: https://github.com/openai/codex/blob/8703d32bd0ea5cf26768859a3dd476df906ba053/codex-rs/core/src/session/handlers.rs
[codex-admission]: https://github.com/openai/codex/blob/8703d32bd0ea5cf26768859a3dd476df906ba053/codex-rs/core/src/session/turn_input.rs
[codex-features]: https://github.com/openai/codex/blob/8703d32bd0ea5cf26768859a3dd476df906ba053/codex-rs/features/src/lib.rs
[codex-wait]: https://github.com/openai/codex/blob/8703d32bd0ea5cf26768859a3dd476df906ba053/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs
[kimi-tasks]: https://github.com/MoonshotAI/kimi-code/blob/725194d7e127811ed754dde0e98c4788866d779f/packages/agent-core-v2/src/agent/task/taskService.ts
[kimi-loop]: https://github.com/MoonshotAI/kimi-code/blob/725194d7e127811ed754dde0e98c4788866d779f/packages/agent-core-v2/src/agent/loop/loopService.ts
[kimi-wait]: https://github.com/MoonshotAI/kimi-code/blob/725194d7e127811ed754dde0e98c4788866d779f/packages/agent-core-v2/src/agent/tools/task/task-wait/taskWaitTool.ts
[kimi-tests]: https://github.com/MoonshotAI/kimi-code/blob/725194d7e127811ed754dde0e98c4788866d779f/packages/agent-core-v2/test/agent/task/idle-notification-repro.test.ts
