---
title: cli xanac
type: doc
status: active
---

# Xanac 客户端

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

DaemonClient 使用 HTTP 提交与取消、SSE 订阅和 Last-Event-ID 断线续传。
Textual 按真实 run_id 保存卡片状态，Esc 停止当前回答；取消按钮发送原 sid/rid。
客户端不提供后台切换或 attach tab。

oneshot 每次生成 input_id，先订阅再提交；按 turn_started/input_accepted/命令回执绑定 run，
只接受匹配 run 的终态。其他 run 的完成事件不能提前结束当前命令。

测试：`test_xanac_oneshot_daemon.py`、`test_xanac_textual_daemon.py`、`test_xanac_textual_flicker.py`。
