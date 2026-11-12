---
title: context
type: doc
status: active
---

# ContextSDK

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`context/sdk.py::ContextSDK` 是上下文生命周期的唯一所有者：`snapshot`、`append`、`replace`、
`append_history`、`restore`、`compact`，以及 per-sid `maintenance` 锁。缓冲和锁直接由此类持有，
原独立 `_ContextStore` 与外部 `MainCompaction` 已删除。run 状态和待处理输入仍归 TurnCoordinator。

消息类型：UserMessage、AssistantMessage、ToolResultMessage、CompactSummary、ContextNote。
用户输入 ID、媒体引用、assistant 的 reasoning 和工具关联 ID 必须往返保留。
ContextNote 的 `to_metadata/from_metadata` 只转换当前消息元数据，不提供旧执行协议。
CompactSummary 的 run_id、transcript_path、revealed_tools 都是明确字段。

SDK 是主日志的可重建视图；HistoryMirror 只提供持久后端契约，在写入、恢复与压缩操作中显式使用。
正常写入经 `append_history`，先落主日志，再更新视图；重复行从主日志恢复视图。后端提交后报错时也重读
主日志校准视图并继续抛错。恢复统一走 `restore`，请求组装只读取隔离的 `snapshot`。

写入和读取均隔离嵌套可变数据，包括工具参数中的字典/列表，组装过程不能反向修改历史。
`compact` 内部冻结前缀与行 ID，向摘要函数提供独立值；通过长度校验后保存完整原始转录、持久替换前缀，
最后更新缓冲并保留尾部。提交前再次在写锁下校验前缀；生成失败或取消不修改历史，提交期间取消等待收尾。
压缩期间的模型重试只生成临时请求，不持有另一份可写的上下文，也不自行提交。

测试：`tests/context/`、`test_sdk_bridge_roundtrip.py`、`test_p3d_sdk_canonical.py`、
`test_compact_persistence.py`；覆盖嵌套数据隔离、前缀竞争、持久化失败/提交后报错、取消与重建。

2026-11-09 迁移验收：全量离线回归 1295 项通过；原 `MainCompaction`、`begin_main_compaction`、
`replace_prefix`、`_ContextStore` 与默认分批转录路径均无生产/测试引用。未部署，未做真实模型缓存命中率验收。
