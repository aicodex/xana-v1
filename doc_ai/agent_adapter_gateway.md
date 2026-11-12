---
title: agent adapter gateway
type: doc
status: active
---

# Gateway

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`DefaultAgentGateway` 负责渠道注册、入站路由、命令和事件渲染，不维护第二份在途 run 表。
`control: TurnCoordinator` 由 boot 与 DefaultHarness 共享注入。`list_inflight` 合并 control 的
活动状态和 RunStore 的持久记录，live 表示当前进程真正执行中。

普通输入交给 agent.send；忙碌会话的接收、排队和停止只在 control 判断。
`accept(inbound)` 返回持久准入结果，网关持有后续事件渲染直到结束，供后台和调度通知使用；
它与 submit 共用执行链，接收成功不表示模型已回答。
`cancel(sid,rid)` 仅停止匹配活动 run；成功后发 run_stopping，等真正取消事件终态。
`resume` 只允许本 sid 的已停止 run，不重发原用户消息、不改名原 trace。
`recover_inputs` 在服务渠道就绪后恢复尚未消费的持久输入。

命令结果有自己的 command_run_id；压缩有独立进度/取消卡。
`cancel_all` 供停机使用：关闭入场，再请求所有活动 run 收尾。
`has_active_runs` 查询同一 control 及尚未结束的交付观察任务，供停机等待收尾；不存在第二份 run 表。
渠道绑定由 SessionResolver 管理并交给 Adapter.bind_session，网关不维护第二份绑定表。

测试：`test_single_loop.py`、`test_gateway_cancel_sets_exit_reason.py`、`test_command_card.py`。
