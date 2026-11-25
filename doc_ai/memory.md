---
title: memory
type: doc
status: active
---

# memory

## 定位

`memory` owns runtime context authority, durable history mirror, and project long-term memory storage/retrieval/injection.

项目记忆(跨会话的长期记忆)在项目目录的 `memory/`:一条记忆一个文件(`name` / `description` / `type` / `topic` / `pinned`);每个会话强制注入索引与置顶记忆,每条消息与 `memory_search` 工具统一经 **SAG2 + 本地 Karpo 4B (`karpo-decision-4B`)** 证据评分检索并召回全文;模型侧唯一写入口是 `memory_write`,每轮结束后另有后台提取兜底漏记。旧的知识树(`MarkdownMemoryTree`)、旧 FTS(`SQLiteMemorySearchEngine`)与旧选择器(`ModelMemorySelector` / `lexical_rank`)已彻底删除。设计见 [memory-sag2-design](../docs/design/memory-sag2-design.md)、[memory-dream-design](../docs/design/memory-dream-design.md)。

## 详细设计

- [memory_active_context](memory_active_context.md) — `ActiveContextStore`，当前 turn 可见上下文的运行时权威
- [context](context.md) — `ContextSDK`，active context 的运行时载体与有序消息与前缀压缩协议
- [memory_history_mirror](memory_history_mirror.md) — `HistoryMirror` / `MemoryReplayer`，主日志持久写入和恢复输入
- [memory_sag2](memory_sag2.md) — SAG2 + Karpo 4B 检索基线(`ProjectMemorySearch`、`ProjectMemoryInjector`、`JevEvidenceJudge`、`memory_search`)
- [memory_rules_gate](memory_rules_gate.md) — `memory_write`:项目记忆(`memory/`)唯一写入口、存储格式、旧记忆迁移
- [memory_extract](memory_extract.md) — 每轮结束后的后台记忆提取(所有运行,子 agent 除外)

## 全局合约

- [README_ai](README_ai.md) — L0 + 7 层架构总览
- [modular_interfaces](modular_interfaces.md#6-stage-5memory--sag2srcxanamemory) — L5 全部 ABC 签名与契约速查
- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- Active context authority.
- Durable history mirror.
- Project memory authoritative Markdown store (`memory/**/*.md`).
- SAG2 retrieval and turn-start injection contracts.

## Does Not Own

Agent behavior, LLM calls, adapter protocol, or tool execution.

## Rules

- Active context is the runtime authority.
- History is the durable conversation authority; SDK is its rebuildable runtime view.
- Normal runtime reads active context, not history.
- Cold start may replay history to rebuild active context.
- Markdown memory content is source of truth; SQLite/SAG2 indexes are rebuildable views.
- 项目记忆(`memory/*.md`)是记忆的唯一权威,索引由它实时生成;模型侧唯一写入口是 `memory_write`(裸 Write/Edit 进 `memory/` 被 policy 拒绝),后台提取经 `ProjectMemoryStore` 只新建或追加。
- L1 自动召回(`ProjectMemoryInjector`)与 L2 `memory_search` 工具共享同一个 SAG2 `ProjectMemorySearch` 实例;评分失败明确报错,不回退其他检索实现。

## Inputs

- context updates
- message rows for mirror append
- replay request at boot
- search queries and user turn text

## Outputs

- active context snapshot
- append acknowledgement
- replay result
- search hits
- injected prompt blocks

## Stage 5 模块化与可观测性

- **正式契约与 ABC**：`src/xana/memory/contracts.py`（`MEMORY_SCHEMA_VERSION = 1`、`MemorySnapshot`、`RetrievalContext`、`RetrievalPlan`、`MemoryWriteOutcome`、`ExtractionOutcome`）与 `src/xana/memory/interfaces.py`（`MemoryStore`、`MemoryRetrievalPolicy`、`MemoryExtractionPolicy`），黄金契约锁定在 `tests/golden/memory_contract_v1.json`。
- **快照/分叉/回滚**：`ProjectMemoryStore` 实现 `MemoryStore` ABC，提供 `snapshot()`、`fork(target_root)`、`rollback(snapshot)` 支持离线 RSI 评测隔离与回滚。
- **可替换策略与解耦**：`DefaultMemoryRetrievalPolicy`（带 `ComponentVersion`）承载 SAG2 召回与预算裁剪，`ProjectMemoryInjector` 通过策略槽委托；`ModelMemoryExtractor` 实现 `MemoryExtractionPolicy` 并依赖 `MemoryStore` ABC；`MemoryWriteTool` 依赖 `MemoryStore` ABC。
- **轨迹与诊断日志**：`MemoryTapeRecorder` 记录 `memory.{recall,search,write,extract,snapshot}` 事件；`DiagLogger("memory")`（`XANA_DIAG=memory`）输出性能/异常诊断日志。
- **独立运行与离线测试双**：`FakeEvidenceJudge`（`src/xana/memory/testing.py`）与 `python -m xana.memory bench [--json]` 脱离宿主与在线模型服务即可独立运行全链路验证。
