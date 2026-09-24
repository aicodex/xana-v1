---
title: harness turn controller
type: doc
status: active
---

# harness turn controller

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/harness_sdk/interfaces.py) — `TurnController` ABC
- [contracts.py](../src/xana/harness_sdk/contracts.py) — `AbortSignal`
- [default.py](../src/xana/harness_sdk/default.py) — 实际的 turn 状态转换内联在 `_invoke` / `_run_query_loop`

## 职责

Turn 状态转换的合约层。状态权威是 `state.RunStore` / `RunRecord`，harness 不持有。

## 仅接口合约，无实现

`TurnController`（start / checkpoint / pause / resume / cancel / finish / fail / status）是 ABC，**没有任何实现类**。实际行为：

- **start / finish / fail**：`DefaultHarness._run_query_loop` 开头经 `StateRepository.runs()` 建 `RunRecord(status="running")`，结束时 `update_usage` + `update_status("completed")`；出错路径发 `error` 事件。crash 恢复时 state 层的 startup_scan 把孤儿 run 标 paused（见 [state_resume](state_resume.md)）。
- **pause / resume**：无运行时 pause 实现——`paused` 只出现在 `HarnessEventKind` 枚举与 RunStatus 中，作为恢复/中断的持久化状态，不由 harness 发出。
- **cancel**：运行态取消走 `AbortSignal`（可变 dataclass，`trigger(reason)` 翻 `aborted`）。loop 每轮开头与 model streaming 循环内轮询；工具执行中的中止见 [harness_tool_dispatch](harness_tool_dispatch.md)。命中后发 `cancelled` 事件，并向 extension 触发 `on_turn_abort`。
- **checkpoint**：无调用方，合约预留。

## 不变量

- AbortSignal 必须可变：turn 进行中 gateway 触发取消，需要能被 loop 里已捕获的实例看到。
- extension turn 生命周期：正常结束发 `on_turn_stop`，取消发 `on_turn_abort`，均经异常隔离（hook 抛错只记日志）。
- **Stop 决策**（`DefaultHarness._stop_decision`，照 Claude Code 2.1.281）：模型以纯文本结束一轮时按序判断——只有思考无可见输出 → 推 1 次；`stop_reason == "length"`（输出被截断）→ 续写 ≤3 次；`on_turn_stop` 返回非空字符串 → 以 `stop_hook` note（"Stop hook 反馈:{理由}"）续跑，连续拦截 ≤8 次，hook 输入带 `final_text` 与 `continuation_active`。续跑以 ContextNote 追加并发 `continuation` 事件。
- 决策中已以 final 调用过 `on_turn_stop` 且放行时，收尾不再重复调用。
