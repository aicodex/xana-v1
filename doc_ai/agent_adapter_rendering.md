---
title: agent adapter rendering
type: doc
status: active
---

# 渠道卡片

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

UnifiedCardState 是平台无关数据；apply_event 处理 HarnessEvent、AgentEvent、ProgressEvent。
Card 管理刷新节流和终态守门；Feishu、Telegram、CLI renderer 输出平台格式。
每个 run 一张回答卡，终态 final/error/cancelled 直接完成该卡；compact_started/progress/finalized
维护独立压缩卡。运行、完成、失败与取消都可独立渲染，不依赖额外回填事件。

顶部唯一回答动作是 cancel，绑定原 run。run_stopping 撤按钮并显示停止收尾；旧卡取消仍定位
原会话。TG 取消回调用固定长度摘要索引原卡，不能截断真实 run ID。
工具 detailed/grouped/summary 模式、工具展开与答案分页继续使用 process_view 规则。

Adapter.note_progress 是正式接口；AdapterProgressSink 转发 usage 和三个 compact 生命周期事件。
配对校验仍在渠道边界执行，取消按钮不绕过它。

测试：`test_card.py`、`test_unified_card_*.py`、渠道 integration 与单循环取消回归。
