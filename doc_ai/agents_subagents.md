---
title: agents subagents
type: doc
status: active
---

# 子 agent 与后台任务

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

正式契约为 `extension_api/capabilities.py` 的 AgentRunner；boot 注入唯一 GatewayAgentRunner。
它直接调用 agent.send 并读完生命周期，在独立子会话运行，返回 AgentRunOutcome 和工具统计，
不发送渠道卡。父子关系写 SessionRecord/RunRecord；子会话 resume 在原上下文继续。

agent_run 工具解析目标、工具上限和技能可见性；上限由 `agents/delegation.py` 的当前规则收窄。
它先等待一段时间；超时或用户插话时返回后台任务 ID。task_wait 只观察该任务，task_stop 才停止它。
agent_run 初次等待期间取消会停止直接拥有的子执行；已经交出的任务继续运行。
GatewayAgentRunner 自身被取消时，通过 Agent.cancel 的原 sid/rid 停止子执行并等待收尾。

BackgroundTasks 持有任务状态、结果和交付状态。工具结果或注记写入主历史后才确认；
父回答结束后可以独立通知会话；父回答取消后结果保留到用户下一轮，避免自动复活。
通知失败不标 delivered；重启保留完成但未送达的结果，运行中的任务标 interrupted。
完整的稳定通知身份、持久输入回执和服务重试流程见[后台任务](harness_background_tasks.md)。

测试：`test_single_loop_waits.py`、`test_background_tasks.py`、`test_agent_delegation.py`、`test_lead_research.py`。
