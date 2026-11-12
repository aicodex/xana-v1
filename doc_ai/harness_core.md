---
title: harness core
type: doc
status: active
---

# Harness 核心

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`DefaultHarness.invoke()` 通过 `TurnCoordinator` 接收输入；一个 sid 同时只有一个 run。
执行 task 归服务持有，观察流关闭只释放观察资源。已运行会话收到新消息时返回
`input_accepted`，在模型响应或工具结果完整写回后消费输入，不启动第二个并行回答。

`default.py` 负责模型/工具循环、Stop 决策、扩展钩子和收尾；`turn_control.py` 负责
入场、持久输入队列、精确取消和边界维护操作；`stream_control.py` 处理静默模型取消。
`HarnessContext.control/run_writer` 是类型化依赖，取消不经隐藏 metadata 信号注入。

主历史先持久追加再更新 SDK；输入回执在消息和上下文注记写入后确认。
终态在 RunRecord、trace 与 owned execution 收尾后对外发出。
预算、max_turns、verify、goal 模式和记忆提取仍走同一执行链。

独立任务的等待、结果确认与完成唤醒见[后台任务](harness_background_tasks.md)。

测试：`test_single_loop.py`、`test_single_loop_waits.py`、`test_default_harness.py`。
