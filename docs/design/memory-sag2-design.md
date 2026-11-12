---
title: SAG2 用于项目记忆检索：源码核查与接入方案
status: implementing
date: 2026-11-10
---

# SAG2 用于项目记忆检索

用户方向：将 SAG2 的词项检索、上下文判断和多轮扩展作为 memory 的核心召回机制；以记忆检索为目标，评分后端不能绑定 JEV。用户已授权算法接入。本次已实现可独立验证的算法、项目索引和评分协议；尚未替换运行时、迁移记忆或修改生产配置。评分指令的来源例外待用户确认，不能将本阶段视为迁移完成。

## 结论

建议接入 **SAG2 算法核心**，由 Xana 管理项目记忆、索引、取消与用量；SAG-Wiki 的 CLI/MCP 保留为已有外部知识库的独立接入选择，不作为内置 memory 的必需运行服务。先建立与上游一致的离线基线，再按记忆任务验证，而非把论文成绩直接等同于 Xana 的效果。

JEV 是可配置的证据评分后端，不是记忆存储，也不等于聊天模型。更换 JEV/reranker 不应重建词项索引，或迫使主聊天模型同时改变。

## 核查范围与版本

| 材料 | 固定版本 | 本次核查 |
| --- | --- | --- |
| [SAG2 论文](https://arxiv.org/html/2610.22588) | arXiv:2610.22588v1，2026-10-15 | 算法、上下文评分、扩展、局限；不把多跳 QA 基准当作记忆评测 |
| [SAG-Benchmark](https://github.com/Zleap-AI/SAG-Benchmark/tree/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2) | `bb7f82751ba1576219f9d9ebd021128603d130c3` | Python 索引/检索、原生 BM25、分段、JEV 与本地评分替换、冻结回归 |
| [SAG-Wiki](https://github.com/Zleap-AI/SAG-Wiki/tree/61f63c660c66f9600bee6c236c3e9b079f90e105) | `61f63c660c66f9600bee6c236c3e9b079f90e105` | Rust 检索/评分、CLI/MCP、配置、来源定位、取消与重试 |
| Kimi Code | `8dd3aa3789a21778261457bba5e56a758583e8c0`，2026-11-10 远端 HEAD | 已 fetch 并通过 git show 核查普通 queue、input.steer、任务等待；未覆盖本地工作分支 |

本地上游副本位于 `/home/xzp/project/best_agent/SAG-Benchmark` 与 `SAG-Wiki`。没有安装 SAG 服务、接入真实 JEV 或下载本地 reranker 权重。

## SAG2 实际做什么

1. 原文经过规范化、分词与有来源位置的分段，建立 SQLite FTS5 及词频统计；不在建库时调用 embedding 或聊天模型。
2. 初始问题通过词项检索取得候选。
3. 评分后端结合原始问题、当前候选及已有证据，判断候选自身是否提供必要证据。关联事实来自上下文，不代表候选本身也包含该事实。
4. 从已选段落提取新词，继续词项检索；新证据参与后续判断，旧判断也可修正。
5. 去重、保留必要种子和初始候选，最后做本地多样性排序，输出原文依据。

关键词扩展主要是本地词频/逆文档频率、与原查询共句关系、名称短语及种子分数的组合；不是每跳再让生成模型编一组搜索词。实现见 [`sag2.py::expansion_terms`](https://github.com/Zleap-AI/SAG-Benchmark/blob/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2/scripts/sag2.py#L585) 与 [`search_sag2`](https://github.com/Zleap-AI/SAG-Benchmark/blob/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2/scripts/sag2.py#L930)。

**免 embedding 不等于完全不分段。** [`chunking.py`](https://github.com/Zleap-AI/SAG-Benchmark/blob/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2/scripts/chunking.py) 根据标题、句子、代码块和原文范围处理文本，长句不会为了目标长度被强行截断。用户无需预制知识图谱、语义树或 embedding，但检索仍有文本单元。

论文的检索质量依赖候选、评分和扩展整体；词项接入仍可能漏掉无共同词项的别名、改写和跨语言表达。不能据此宣称所有记忆任务必然胜过 embedding，或把建库耗时当成在线召回延迟。

## 直接接 Wiki 还是接算法

| 方案 | 可复用部分 | 对 Xana 的代价 | 建议 |
| --- | --- | --- | --- |
| SAG CLI/MCP 独立服务 | 成熟索引、文件监听、检索、来源定位；CLI 不依赖桌面端 | 额外服务、用户级数据目录/配置/密钥、进程恢复与项目隔离；现有 npm 文档的预编译平台是 macOS Apple Silicon/Windows x64，Linux 需另核构建发布 | 用于用户已有的外部知识库 |
| 复用 Python SAG2 算法 | 可读、可冻结回归的索引与检索实现，便于嵌入现有 Python 生命周期 | 需工程化封装、增量索引、取消、用量与评分契约；上游是 benchmark 脚本，非现成稳定 Python SDK | **内置 memory 推荐** |
| 直接绑定 Rust sag-core | Wiki 生产索引与检索的原生实现 | Python 绑定/发布与 Rust 依赖边界；core 还依赖文档转换、服务及 agent 组件，不只是一个检索函数 | 首版不采用 |

SAG-Wiki 已有纯检索入口，不必调用它的问答模型：CLI 的 JSON 搜索与 MCP `search` 都可以返回证据。不能把“它是完整应用”误读成“只能给最终答案”。源码：[CLI 文档](https://github.com/Zleap-AI/SAG-Wiki/blob/61f63c660c66f9600bee6c236c3e9b079f90e105/docs/en/cli-and-harness.md)、[`sag_mcp.rs:379`](https://github.com/Zleap-AI/SAG-Wiki/blob/61f63c660c66f9600bee6c236c3e9b079f90e105/core/src/sag_mcp.rs#L379)。

Wiki 的检索函数本身接受评分回调，说明算法与评分可分离；但公开 JEV 配置的 URL/model 可改，不代表请求协议已自动兼容任意聊天模型或 reranker：[`retrieval.rs:181`](https://github.com/Zleap-AI/SAG-Wiki/blob/61f63c660c66f9600bee6c236c3e9b079f90e105/core/src/retrieval.rs#L181)、[`models.rs:229`](https://github.com/Zleap-AI/SAG-Wiki/blob/61f63c660c66f9600bee6c236c3e9b079f90e105/core/src/models.rs#L229) 使用 Noul 问题与分数响应。

Python 基线也不是复制几句 SQL 即可：原生 `sag_bm25` 扩展、中文分词、词项统计、候选保留、上下文评分和最终去重都影响结果。若简化实现，应标明差异并验证；不得宣称原样复现。移植需保留 MIT 版权与许可证。上游 `pyproject.toml` 为 `package=false`，不能当作已经发布的稳定依赖直接加入。

## Xana 现状与真正需要替换的部分

- `ProjectMemoryStore` 管理 `memory/*.md`，保留用户、反馈、项目、参考资料等类型及置顶；文件仍是唯一权威。
- `ProjectMemoryInjector` 当前把全部记忆的名称/描述交给选择模型，挑选后再注入正文；失败回退字面匹配。这不是 embedding 检索。
- `SQLiteMemorySearchEngine` 已有 FTS5/BM25 候选，但最终分数又按查询词重叠计算；`memory_search` 使用它，自动 recall 不使用它。
- 两条路径因此不能共享候选、检索证据、成本与扩展状态。现有声明中“L1/L2 共用同一检索底座”尚未真正落实。

直接调用方：`memory/recall.py`、`memory/markdown.py`、`memory/store.py`、`runtime/memory_selector.py`、`runtime/boot.py`、`tools/builtin/memory_search.py`、`memory_write.py` 与写后索引钩子。现有正式契约位于 `memory/contracts.py`、`memory/interfaces.py`，应优先原位更新，不新增另一套 mem 工具或隐藏兼容召回器。

## 以 memory 为目标的边界

### 数据和作用域

默认检索项目 `memory/`；明确接入的知识库单独设 scope。不把整个代码仓库、所有原始聊天和运行日志自动混成“已确认记忆”。SQLite 是可重建索引，不成为第二个可写记忆库。

小记忆可保留全文，长记忆按自然标题/句子生成可追溯单元。每个单元关联文件路径、内容哈希、源范围、类型与现有更新时间。修改、删除、重命名后增量更新，不能只追加新版本而留旧命中。

scope 必须在初始查询、词频统计、扩展和评分之前应用，不能先全库 top-k 再过滤；项目隔离也不能只依靠传给模型的文字。索引版本或文件哈希在查询期间变化时，结果需显式失效/有界重试。

### 规则、偏好和证据

SAG2 的原问题是必要证据判断；记忆还包括用户偏好、禁止事项、已跑通的工作流与失败经验，它们未必是“直接回答问题的事实”。

已明确必须遵守的规则与现有 pinned 通路先保留，不交给相关性分数淘汰。其他记忆的判断目标是对当前任务是否有帮助，包括中间连接、方法和失败记录；不能只看是否含答案。全文中的指令对评分器只是待判断数据，不授予工具权限。

新旧记录冲突需要保留来源与时间；高相关度不是新事实的真值认证。先沿用现有 deprecated/类型/时间语义，不为算法预建另一套复杂记忆治理系统。

### 返回契约和上下文

召回返回原文片段及 source path/hash/range、评分来源和检索轨迹，不让 JEV 生成一段代替原文的“记忆总结”。自动注入与 `memory_search` 共用同一个检索实例；前者施加上下文预算，后者提供进一步读取来源的入口。

命中结果由现有 ContextSDK 按用户输入身份一次提交；多跳中间状态不反复塞进聊天历史，不改旧前缀。是否去除现有全量入口索引另列影响，不与检索替换同时盲删。

## 可配置评分后端

建议在现有 `memory/contracts.py` 定义评分请求/结果的数据契约与类型化 Callable，在 `boot()` 注入具体后端；这里不批准或预建新 ABC。

- 输入：原始用户任务、当前查询、带稳定 ID 的候选正文、已找到的证据与索引版本；只传当前项目允许的范围。
- 输出：与候选 ID 严格对齐的相关性结果、后端/模型版本及用量。缺项、重复 ID、非有限分数或协议错误明确失败。
- 首个实现使用 JEV Noul；后续接普通模型或本地 reranker 时显式实现适配。不同后端的分数分布不等价，不能沿用同一阈值就声称效果等同。
- 选择项引用现有模型配置目录里的命名条目（例如 proposed `memory.judge_model`），地址、鉴权与模型名不在 memory 配置里重复存一套。切后端不改聊天默认模型，不暗中切 K3。
- JEV 判别协议独立处理，不伪装成生成式 chat-completions 的流事件。是否复用模型目录的解析范围在实施时核查直接调用方，不通过 metadata 或全局单例注入服务。

上游已提供 [LocalScorer](https://github.com/Zleap-AI/SAG-Benchmark/blob/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2/scripts/local_reranker.py#L182) 和[本地 rerank 文档](https://github.com/Zleap-AI/SAG-Benchmark/blob/bb7f82751ba1576219f9d9ebd021128603d130c3/sag2/docs/local-rerank.md)：保留词项扩展，替换每轮评分。它证明有这条适配路径，不证明任意模型都能达到 JEV 基准的精度。

用户说的“后台 JEV”在此理解为评分后端。当前回答要使用的召回必须在请求组装前完成；后台索引更新可独立进行，不能把当前问题的召回拖到回答后再假装已参与回答。

## 长期运行和预算

上游最多两次扩展，不无限递归。其 candidate=100、seed=10 等是参考基线，不直接作为聊天自动召回默认值；必须测中文 mem 的收益、延迟和判断数后确定。

检索总 deadline、候选/判断数、并发、输入与结果体积均需有明确边界；用量接现有观测/预算控制，不另做账户账本。用户取消须传给检索任务和评分请求，不能留下无限重试的后台网络操作。

不能照搬 benchmark 客户端对 499 的无界重试；Wiki 的有总时限/次数/取消实现更适合作为工程参考。缓存需包含候选内容、已有证据、问题、索引版本和评分后端版本；不能只按 query+候选缓存，因为同一候选会随中间证据改变判断。

评分失败明确报告；自动召回可继续保留已有固定记忆并展示召回失败状态，但不得把未评分 BM25 候选冒充已验证记忆。若将来提供纯词项模式，也应是用户显式配置，不是悄悄替代服务故障。建库零模型调用并不等于查询零成本或可承诺 3 秒内完成。

## 实施路径与删除条件

1. **离线基线**：冻结上游版本，采用小型中文记忆集测试“检索—判断—扩展”，保留每步来源和固定评分；核查原生排名扩展与 Python/Linux 发布需求。
2. **独立评分契约**：基于现有接口给出配置与调用方修改清单，先接 JEV；普通模型/本地 reranker 作为后续实现，拒绝缺配置时隐式替换。
3. **统一检索入口**：自动 recall 与 `memory_search` 使用一个实例，更新 memory_write、后台提取和手工文件变更的索引联动；固定规则与 pinned 先保持。
4. **验证后原位切换**：列出已有配置/数据的具体影响并取得实施授权；迁移只重建派生索引，不删源记忆。删除旧名称/描述选择器及不再使用的回退路径，同步更新测试和文档；不能只给旧链再套一层 SAG2。

需要实施前确认的一点：评分准则也是模型可见指令。现有 AGENTS.md 要求查三家上游并译成中文；SAG2 独有的必要证据准则目前只完成 SAG 原文核查。不能以“算法适配”为由自写 memory 评分提示词。后续应单独确认采用 SAG 原文/记忆用途适配的来源范围，遵守已有提示词约束。

验收侧重：中文同义改写、短确认消息、历史偏好、已跑通流程、失败经验、跨文档二跳、无结果、旧记忆冲突、更新/删除与跨项目隔离。比较当前 recall、单轮 BM25、BM25+评分、SAG2+不同评分后端，记录 Recall@k、无关记忆注入、错误旧事实、延迟与判断数；不能只展示几个成功例子。

## 本次验证

- 在独立 `/tmp/xana-sag-review-venv` 安装 jieba/pyuca；原生编译缓存隔离到 `/tmp/xana-sag-review-cache`。源码依赖和打包资源声明已更新；没有安装到生产环境。
- SAG2 上游分段、原生排名、分词及冻结的多跳排名/完整评分载荷回归 **37 passed / 2 warnings / 13.76s**。评分为固定 Oracle，不是实际 JEV；未验证真实网络、在线延迟或中文记忆效果。
- Xana 算法、项目隔离、评分协议、既有 recall/CJK 与架构专项 **48 passed / 2 warnings / 5.43s**；全部后端回归 **1385 passed / 42 warnings / 63.36s**。网络测试使用 MockTransport，不产生模型费用。
- 同步 `uv.lock` 后，以其 regex 2026.11.9 / tiktoken 0.14.0 在独立环境复跑上述专项 **48 passed / 2 warnings / 5.59s**；`uv lock --check` 通过。sdist/wheel 构建成功，核对 wheel 内 MIT 许可、来源记录、原生 C 与两个 SQLite 头文件均存在。默认服务 `/healthz` 返回 `ok: true`，未重启任何服务。
- Xana 的排队/选择性插话专项另行核查；此次没有为更改 memory 去动对话队列或运行服务。

补核排队：最新 Kimi 的 `promptChannel.ts`、`human/agent/machine.ts`、`loopService.ts` 与 `taskWaitTool.ts` 和此前核查版本在这些文件上无差异。普通 submit 排到整轮结束；steer 将选中消息移入通知队列，在下一 drain 边界提交。`human/test/agent/machine.test.ts:1308` 明确先完成 slow_tool，再写入提升的用户消息，再生成后续答案。只有观察等待接受独立 steerSignal；不是中止整个执行。Xana `test_input_queue.py` **10 passed / 0.93s**，含真实 Bash 保持运行与后台等待唤醒。

## 已落地的独立阶段与剩余工作

模块入口、依赖、状态所有权和测试见 [memory_sag2](../../doc_ai/memory_sag2.md)。

已实现：固定版本的检索算法与 MIT 来源记录、项目 `memory/**/*.md` 扫描及 SQLite 派生索引、作用域前置过滤、内容哈希验证、评分 Callable 契约、JEV Noul 有界请求适配。没有内置评分提示词或默认远端地址，没有增加运行时选择分支或隐藏回退。

尚待完成：评分指令来源确认、正式模型配置及 `boot()` 组装、自动召回与工具共用实例、旧选择器及不再使用路径删除、真实评分服务验收。切换时只重建索引，源记忆不删除。当前新增实现尚无生产调用方；不是获准长期并存的第二条业务路径。

接入前还需解决初次建库/本地扩展同步计算占用事件循环的问题，验证大型记忆集与取消延迟；当前查询 deadline 只能在异步让出时生效。现有实现按所选来源快照整体重建变化的索引，尚不是逐文件增量更新。需要测量后再决定是否增加增量复杂性，不能将当前更新能力描述为逐条增量。

首次原生排名扩展构建需要 C 编译器及允许加载扩展的 SQLite；原生源码和头文件随包发布，缓存可重建，缺依赖明确失败。跨平台预编译产物与无编译器安装尚未验收。

SAG2 返回候选排序；是否把低分候选注入、中文记忆的评分校准、固定规则和嵌套记忆写入语义，均须在自动 recall 切换前明确并验证。离线 Oracle 一致不等于真实记忆质量已经合格。
