---
title: runtime lifecycle
type: doc
status: active
---

# 服务生命周期

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

serve 获取每项目实例锁 → boot → 渠道与 daemon → 调度轮询 → 恢复后台结果和待处理输入。
每个渠道 pump 独立接收入站消息；每条输入单独提交，使忙碌会话能立即接收插话。
后台任务登记表在 boot 构造时先加载；渠道就绪后启动通知恢复和重试任务，退出时取消该任务。
结果通知的接收与后续渠道渲染分开，均走现有 gateway/control，见[后台任务](harness_background_tasks.md)。

HTTP 保留 submit、stream、cancel、button；没有 attach/detach 动作端点。
SSE 观察断线不会取消服务持有的执行；Last-Event-ID 继续从 ring buffer 恢复事件。
原有 Origin、Bearer、Unix socket 权限边界保持。

SIGTERM 先停止调度器领取新任务，等待已领取的任务执行并记录结果，再关闭会话入场并请求
活动 run 停止，最多等 30 秒收尾，最后关闭 pump、daemon 和渠道。已领取的一次性任务不能
在写执行结果前被进程退出吞掉。迁移不重建 scheduled_tasks.json。

测试：`test_single_loop_shutdown.py`、scheduler/serve/daemon 相关测试与单循环回归。
