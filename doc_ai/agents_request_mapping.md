---
title: agents request mapping
type: doc
status: active
---

# Agent 请求映射

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`AgentRequestMapper` 把 AgentRequest 转成 HarnessRequest 和 HarnessContext；EventMapper
把终态同名透传，tool_finished 映射为 tool，其余为 draft，并保留完整 HarnessEvent。

DefaultAgentRequestMapper 负责媒体快照与模型/工具/权限/cwd 配置。run_id 采用调用方明确 ID，
否则生成独立 UUID；新的 HarnessContext 创建自己的 AbortSignal。run_writer 是类型化构造依赖。
忙碌会话的补充输入由 control 接收到原活动 run，执行继续使用该 run 的配置快照。

会话配置覆盖 agent/default 的模型、effort、streaming、thinking、search_provider 与 view_mode。
模型窗口、输出、摘要上限、压缩预算、媒体能力和缓存 TTL 从所选 ModelConfig 读取。
EnterWorktree 的 cwd 在下一 run 生效；目录丢失明确失败。

`GatewayAgentRunner` 取消等待时调用 Agent.cancel(sid,rid)，不通过 metadata 传可变取消对象。
测试：`test_lead_research.py`、`test_multimodal.py`、`test_enter_worktree_tool.py`。
