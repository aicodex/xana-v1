---
title: state resume
type: doc
status: active
---

# 重启与恢复

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

`startup_scan.scan_runs_dir` 检查 run trace 的终止 marker；`mark_unfinished_as_resumable`
将没有正常终止的记录标成 paused / shutdown_force。主上下文只由主日志重建，不自动重放任务。
已取消 run 的原 trace 和部分输出留存。`/resume <id>` 由 gateway 检查会话与停止状态，
启动新 run，使用当前主历史，给缺少结果的工具调用写“结果未知”说明。

`RunStore.list_unfinished` 的可列出范围仍由现有 exit_reason 集合控制。
已提交压缩的 v2 检查点是摘要标识；恢复不需要私有检查点视图或原始输入重发。
尚未消费的输入由 TurnCoordinator 的持久接收队列恢复，使用 input_id 去重。pending_requests 返回全部待处理请求，gateway 通过原 request mapper 重建运行依赖；普通 queue 按序逐条启动，steer 状态保留，不回到旧的默认自动插话语义。

旧数据迁移脚本：`scripts/migrate_single_loop.py`；运行内核不包含旧执行格式转换路径。
测试：`test_compact_resume_integration.py`、`test_single_loop.py`、`test_single_loop_migration.py`。
