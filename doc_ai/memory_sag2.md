---
title: memory_sag2
status: implementing
---

# 项目 SAG2 检索

当前是可独立验证的接入阶段，尚未由 `boot()` 启用。运行中的自动 recall 和 `memory_search` 仍使用原实现；下一阶段需原位替换并删除旧选择器，不新增兼容开关。评分指令来源例外待用户确认，依据及完整迁移方案见 [memory-sag2-design](../docs/design/memory-sag2-design.md)。

## 入口与契约

- `memory/contracts.py`：`EvidenceCandidate`、`EvidenceJudgment`、`EvidenceJudge` Callable、`EvidenceCriterion` 与 `SAG2Options`；没有新增 ABC。
- `memory/project_search.py::ProjectMemorySearch`：实现既有 `MemorySearchEngine`。构造注入项目根目录、索引位置、必需评分器、查询时限和算法参数；`search(SearchQuery)` 返回 `SearchHit`。
- `memory/sag2/`：固定版本上游的分词、自然分段、FTS5/原生 BM25、上下文评分循环、词项扩展和多样性排序。`NOTICE.md` 记录来源和移植边界，MIT 许可随包保留。
- `runtime/memory_judge.py::JevEvidenceJudge`：JEV Noul 协议适配；客户端、地址、模型、评分准则、并发/重试/时限均由调用方注入。评分准则没有默认值；后续只在 `boot()` 选择和组装后端。

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
