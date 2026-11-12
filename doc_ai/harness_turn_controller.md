---
title: harness turn controller
type: doc
status: active
---

# harness turn controller

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [turn_control.py](../src/xana/harness_sdk/turn_control.py) — 同会话单执行、队列、promote、取消与恢复
- [contracts.py](../src/xana/harness_sdk/contracts.py) — `AbortSignal`
- [default.py](../src/xana/harness_sdk/default.py) — 实际的 turn 状态转换内联在 `_invoke` / `_run_query_loop`

## 职责

执行准入、输入队列、状态转换与取消。TurnCoordinator 拥有活动 run、持久待处理输入及消费回执；RunStore / RunRecord 保存执行记录。

## 状态转换（内联在 DefaultHarness）

不设 turn 控制器 ABC，各状态转换内联在 `DefaultHarness`：

- **start / finish / fail**：`DefaultHarness._run_query_loop` 开头经 `StateRepository.runs()` 建 `RunRecord(status="running")`，结束时 `update_usage` + `update_status("completed")`；出错路径发 `error` 事件。crash 恢复时 state 层的 startup_scan 把孤儿 run 标 paused（见 [state_resume](state_resume.md)）。
- **pause / resume**：无运行时 pause 实现——`paused` 只出现在 `HarnessEventKind` 枚举与 RunStatus 中，作为恢复/中断的持久化状态，不由 harness 发出。
- **cancel**：运行态取消走 `AbortSignal`（可变 dataclass，`trigger(reason)` 翻 `aborted`）。loop 每轮开头与 model streaming 循环内轮询；工具执行中的中止见 [harness_tool_dispatch](harness_tool_dispatch.md)。命中后发 `cancelled` 事件，并向 extension 触发 `on_turn_abort`。
- **checkpoint**：harness 不调用。

## 不变量

- AbortSignal 必须可变：turn 进行中 gateway 触发取消，需要能被 loop 里已捕获的实例看到。
- extension turn 生命周期：正常结束发 `on_turn_stop`，取消发 `on_turn_abort`，均经异常隔离（hook 抛错只记日志）。
- **Stop 决策**：只有思考无输出、length、stop hook 和 verify 仍沿现有统一循环。持久 GoalService 替换独立模型评估器；活动目标未请求完成则在同一 run 下一模型步继续，请求完成须经 verify 通过。失败事实写回 ContextSDK；用户取消、明确 max_turns、hook/验证限制使目标暂停，预算达到使其 blocked。Goal 不继承旧评估器的 8 轮上限；hook/verify 自身上限保留。正常交互请求不推断创建 goal；PromptProfile.goal_mode 仍是无人值守目标录入开关，非第二套评估链。
- 决策中已以 final 调用过 `on_turn_stop` 且放行时，收尾不再重复调用。


## 排队与提升（2026-11-10）

普通输入默认排队（queue），在当前 run 终态提交后逐条启动新 run。promote 只把指定 input_id 改为 steer；在原循环下一安全边界写入上下文，并唤醒 agent_run/task_wait。只读队列投影用于 Web/聊天卡片，模型上下文仍经 ContextSDK 提交，不为排队先写一份模型历史。

AcceptedInput 是唯一持久输入状态，delivery 在同一 inputs.json 中保存；已有未标记记录读取为 queue。不另建旧自动插话路径。PreparedInput 仅保存可重建的构造上下文、执行函数和观察流，不是第二份待处理消息权威；启动/提升时移除，重启从正式 request mapper 重建全部剩余输入。服务收尾后不再启动后续队列，聊天输入留待恢复。

promote 与取消分开；普通命令和正在生成的模型响应不被插话中止，工具结果不重放。显式取消只停止当前 run；普通队列随后继续。已提升的输入随旧 run 收尾持久记录。详见[当前会话契约](../docs/design/session-and-card-state.md)。
