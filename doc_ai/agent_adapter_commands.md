---
title: agent adapter commands
type: doc
status: active
---

# 会话命令

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

DefaultCommandRouter 提供 help/h、clear、agent、compact、model、effort、streaming、thinking、
cancel、project、search_provider、view、resume、tasks；隐藏 compact_cancel 只供压缩卡使用。
未知命令明确返回错误。async/attach 已从入口、配置与实现中移除。

clear/agent 轮转 sid，保留旧会话与其执行；旧卡停止绑定原 sid/rid。
compact 经共享 control 排到完整响应/工具边界，有自己的进度卡与取消信号。boot 显式注入 Harness 与 AgentRequestMapper；手动/自动共用 Harness.compact，命令层不再另行拼摘要模型请求。
compact_cancel 必须匹配请求者 sid；不取消普通回答。cancel 无参数只请求停止当前 run。
配置命令仅更新给定字段，活动 run 保持原快照；tasks 合并活动 control 与持久 RunStore。

测试：`test_default_command_router.py`、`test_command_compact.py`、`test_command_card.py`。
