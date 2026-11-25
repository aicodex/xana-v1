---
title: memory_sag2
status: active
---

# 项目 SAG2 检索与召回基线

SAG2 + 本地 Karpo 4B（`karpo-decision-4B --graphs`）已是当前唯一且默认的长期记忆检索与召回基线。旧的 `MarkdownMemoryTree`、`SQLiteMemorySearchEngine`、`ModelMemorySelector` 与 `lexical_rank` 已全部删除；后续 Stage 5 拆分长期记忆子系统直接以此为基线。设计与依据见 [memory-sag2-design](../docs/design/memory-sag2-design.md)。

## 入口与契约

- `memory/contracts.py`：`EvidenceCandidate`、`EvidenceJudgment`、`EvidenceJudge` Callable、`EvidenceCriterion`、`SAG2Options`、`InjectedBlock`、`MemoryRecord`。
- `memory/project_search.py::ProjectMemorySearch`：实现 `MemorySearchEngine`，作为 `BootResult.search_engine`、`MemorySearchTool` (`memory_search`) 与 `ProjectMemoryInjector` 的唯一共享检索引擎。
- `memory/recall.py::ProjectMemoryInjector`：每条用户消息注入 `index`（全量索引）、`pinned`（置顶全文）及 `recall`（检索词 ≥2 且 `mode="sag2"` 时调用 `ProjectMemorySearch`，最多 5 条，每条 ≤2000 字 / 总计 ≤6000 字，超过 1 天附过时提示；`mode="off"` 只注入索引与置顶）。评分失败明确报错，不静默回退。
- `memory/sag2/`：固定版本上游的分词、自然分段、FTS5/原生 BM25、上下文评分循环、词项扩展和多样性排序。`NOTICE.md` 记录来源和移植边界，MIT 许可随包保留。
- `runtime/memory_judge.py::JevEvidenceJudge`：JEV/Karpo System One Noul 协议适配；客户端、地址、模型、评分准则、并发/重试/时限均由 `boot()` 构造注入（对 `127.0.0.1`/`localhost`/`::1` 自动设置 `trust_env=False` 绕过系统代理）。

## 状态和生命周期

项目 `memory/**/*.md` 是唯一原件；不依赖 README 或知识树入口。忽略 deprecated/空文件，拒绝跨目录符号链接。scope 在建立词项统计和评分前生效。

SQLite 记录来源内容与词项统计，是可重建视图；数据库绑定所属项目根目录，误用其他项目的数据库明确失败。文件修改/删除、scope 改变会按来源快照重建，查询结束重新核对哈希。暂未做逐文件增量更新。

每次查询持有本实例的锁并在退出时关闭索引及其读连接池。评分结果必须严格对应候选 ID，分数/用量验证失败即报错，不回退另一种召回。命中保留来源哈希、范围、轮次和评分模型；可通过 `on_query` 观察用量与轨迹，不在查询器内另设账本。

JEV 适配器不拥有注入的 HTTP 客户端；调用方负责关闭。重试有次数上限，取消会取消并等待全部子请求退出；不剪裁原文以掩盖超出评分输入上限。

## 依赖和接入前限制

依赖 jieba、pyuca、regex、tiktoken 和 SQLite FTS5。原生排名扩展首次使用时在 `XDG_CACHE_HOME/xana-sag2/native` 构建，需 C 编译器和 SQLite 扩展加载能力；无静默排名替代实现。

本地建库和扩展计算目前同步执行，首建/大库可能阻塞事件循环；查询时限并不保证能抢占同步计算。正式切换前需处理并验证。尚无真实 JEV、在线性能、中文记忆质量、跨平台无编译器安装验收；默认候选参数来自参考实现，不表示已经选定自动注入阈值。

## 验证

- `tests/test_sag2_kernel.py`：冻结上游词项、排名、扩展和完整评分载荷 Oracle。
- `tests/test_project_memory_search.py`：项目隔离、scope、更新/删除/重开、来源变更、符号链接、错误评分与取消。
- `tests/test_memory_judge.py`：MockTransport 协议、有界重试、错误响应、输入上限和取消。

执行时使用独立测试环境和原生缓存；具体结果见设计文档。测试不连接真实评分模型。

## Karpo 本地评分接入

`config/system_model.py::MemoryJudgeConfig` 提供显式评分配置；`runtime/memory_judge.py::build_evidence_judge` 将 Karpo 或 JEV 配置归一到同一个 JEV Noul 适配器，不改变主聊天模型。默认端点为 `http://127.0.0.1:18440/v1/systemone`，默认模型为 `karpo-decision-4B`（推荐加 `--graphs` 预编译启动），评分明细使用 SAG-Benchmark 固定版本的原文，不改写提示词。

`boot()` 始终构造 `ProjectMemorySearch` 作为唯一 `search_engine`，同时注入 `MemorySearchTool` 和 `ProjectMemoryInjector`（默认 `memory.recall="sag2"`，可设为 `"off"` 关闭自动召回）。评分失败直接报错，不回退其他检索实现。judge HTTP 客户端由 `BootResult.memory_judge_client` 持有，serve 退出时关闭。

配置示例：

```json
{
  "memory": {
    "recall": "sag2",
    "judge": {
      "backend": "karpo",
      "endpoint": "http://127.0.0.1:18440/v1/systemone",
      "model": "karpo-decision-4B"
    }
  }
}
```
