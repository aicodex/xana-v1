---
title: memory history mirror
type: doc
status: active
---

# 主历史与恢复

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`HistoryMirror` 公开 `append(row)->bool`、`read_rows(session_id=None)`、`replay`、
`compaction_prefix`、`compact(row_ids, summary)`。append 按 row_id 去重，返回是否新写入。
FileHistoryMirror 写入主会话 JSONL 并 fsync；压缩原子替换捕获的前缀，保留尾部与其他 agent。
InMemoryHistoryMirror 实现相同契约，供无磁盘运行和测试使用。

普通消息写主日志后更新 SDK；失败重试可从日志重建视图。FileMemoryReplayer 按
(session_id, agent) 分组恢复当前消息。旧运行格式必须先用离线脚本迁移，运行时明确拒绝。
损坏 JSONL 行仍按已有策略报警跳过；离线迁移遇到损坏记录则拒绝修改。

`state/file_runs.py` 的 RunJsonlWriter 另存 run trace 和终止 marker，用于审计、原始产物查看；
它不参与另一条会话回写链。压缩前全文转录另存并由 CompactSummary 引用。

测试：`test_compact_persistence.py`、`test_run_trace.py`、`test_single_loop_migration.py`。
