---
title: protocol
type: doc
status: active
---

# protocol（基础数据原语、Tape 与 Eval 协议）

## 职责

`src/xana/protocol/` 是全仓零业务依赖的基础数据底座（纯 Python 标准库，无第三方依赖、无对其他 `xana.*` 包的导入），负责：

1. **版本与确定性序列化**（`versioning.py`）：双版本头（`SCHEMA_VERSION = 1` / `MIN_READER_VERSION = 1`）、`VersionError`、`assert_reader_compatible`、`MigrationRegistry` 链式升版、严格 `to_wire` / `canonical_json`（排序键、禁止 `NaN`/`Infinity`/活对象/`Path`/`frozenset`）。
2. **最小共享值原语**（`primitives.py`）：`TokenUsage`（幺半群 `.add()`）、`ProtocolError`、`BlobRef`、`ComponentVersion`、`ContentPart`、`Provenance`。
3. **Append-Only 运行磁带**（`tape.py`、`__main__.py`）：`RunManifest`、`TapeHeader` / `TapeEvent` / `TapeClosed`（显式嵌套 `payload`）、`TapeWriter`（Write-Before-Reference CAS `blobs/<sha256>` 外置、截断标记、`lifecycle_reserve` 尾预算保底、防覆盖已有磁带）、`read_tape` / `read_blob`（按 `b"\n"` 切行防 Unicode 行分隔符破坏、防路径穿越与符号链接、坏行/坏 Blob 显式报 `corrupted`）、`slice_tape` / `redact_tape_events` / `diff_tapes`。
4. **适配器无关 Eval 记录**（`eval.py`）：`MetricDefinition`、`ToolRunCounts`、幺半群 `RunSummary` / `RunCoverage`、`EvalTrialRecord`、`BenchmarkSnapshot`（已决样本统计不变式）及 `write_benchmark_snapshot` / `read_benchmark_snapshot`。

## 入口与独立运行

- 包导出：`xana.protocol`（17 个公开 wire dataclass 受 `tests/golden/protocol_contract_v1.json` 锁定）。
- 独立调试 CLI（零业务依赖）：
  ```bash
  uv run python -m xana.protocol tape inspect <bundle_dir>
  uv run python -m xana.protocol tape verify <bundle_dir>
  uv run python -m xana.protocol tape slice <bundle_dir> [--from-seq N] [--to-seq M] [--turn-id ID] [--redact]
  uv run python -m xana.protocol tape diff <left_bundle> <right_bundle>
  ```

## 状态归属与依赖约束

- 仅定义不可变值对象与纯文件磁带/快照读写，不拥有任何运行时会话状态或业务接口。
- 架构门禁（`tests/test_arch_layers.py`）：`xana.protocol` 仅允许导入 Python stdlib 与 `xana.protocol.*`；其他模块允许单向向下依赖 `xana.protocol`；旧根文件 `xana.contracts` 已永久封禁。

## 测试与契约守卫

- 单元、边界、故障、安全与 1 万条事件压测：`tests/protocol/test_{primitives_and_versioning,tape_io,tape_debug_and_edges,eval_protocol}.py`。
- 语义契约摘要与破坏性变更分类器：`tests/contract_guard.py` + `tests/test_protocol_contract_golden.py` + `tests/golden/protocol_contract_v1.json`。
- 架构依赖守卫：`tests/test_arch_layers.py`（AST 扫描，冻结存量 124 条跨包边只减不增）。
