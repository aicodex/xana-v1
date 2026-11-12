---
title: memory active context
type: doc
status: active
---

# ActiveContextStore

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

接口在 `memory/interfaces.py`，默认实现是 `InMemoryActiveContextStore`。
`get/set/append_message/clear/snapshot` 只操作当前消息序列；`snapshot` 返回纯 MemoryMessage。
底层唯一生命周期所有者是 ContextSDK；本适配器只转换 MemoryMessage 与 SDK 消息。
正常持久写入、重放与压缩都由 ContextSDK 执行，经 `sdk()` 使用正式接口，不另设压缩管理器。

调用者不能把运行状态塞进消息序列。活动 run 和待处理用户输入归 TurnCoordinator。
持久写入权威为 HistoryMirror，模型正常读取 SDK，恢复时由 FileMemoryReplayer 重建。

测试：`tests/context/`、`test_compact_persistence.py`、`test_single_loop_migration.py`。
