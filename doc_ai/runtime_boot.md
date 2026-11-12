---
title: runtime boot
type: doc
status: active
---

# Boot 组合点

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

boot() 是唯一注册与组合入口。创建单一 ContextSDK、ActiveContextStore、HistoryMirror，
重放主历史；构造 TurnCoordinator 并共享注入 DefaultHarness、Gateway 与 CommandRouter。
有 history_root 时输入接收队列落在项目 run/inputs.json，run trace 与主历史分开保存。

boot 组装模型、工具、技能、extensions、AgentRunner、BackgroundTasks 与 SchedulerService。
缺少真实模型配置时明确失败；fake 仅可显式配置。模型预算公式与缓存 TTL 保持唯一配置推导。

启动扫描仅标记未完成 run 可恢复。serve 在渠道就绪后恢复后台登记表和未消费输入。
记忆提取和独立调度任务仍保留，不因删除旧会话执行机制而被一起删除。

测试：`test_boot_phase2_wiring.py`、`test_boot_persistence.py`、`test_boot_progress_pipeline.py`、
`test_explicit_fake.py`、`test_single_loop_migration.py`。
