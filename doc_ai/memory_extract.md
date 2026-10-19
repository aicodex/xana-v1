---
title: memory extract
type: doc
status: active
---

# memory extract(记忆的后台提取)

## 上级模块

- [memory](memory.md) — memory 模块设计
- 设计:[memory-dream-design](../docs/design/memory-dream-design.md) §3.1

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `ExtractionRequest`、`MemoryExtractor`
- [memory_extract.py](../src/xana/runtime/memory_extract.py) — `ModelMemoryExtractor`(提示词、写入规则)
- [default.py](../src/xana/harness_sdk/default.py) — `DefaultHarness(memory_extractor=...)`、`_start_memory_extraction`
- [boot.py](../src/xana/runtime/boot.py) — 组装(模型注册先于 harness 构造)

## 职责

照 Claude Code `extractMemories`:每轮正常结束(final)后,把本轮(用户消息起到最终回答)交给后台任务,用一次关闭思考的小模型调用找出值得长期记住、但没被记下的东西,写进项目记忆。与系统提示词里的"什么时候存记忆"(只在聊天会话)互补:主动记靠模型自己,后台提取兜底。

- **范围**:所有带 memory 工具组的运行——聊天会话、定时任务等无人值守运行;`agent_run` 派出的子 agent(请求带 `parent_run_id`)不提取。
- **跳过**:本轮模型已调用过 `memory_write`;`memory.extract_every`=N 时每个会话每 N 轮提取一次;0 = 关闭(boot 不注入提取器)。
- **输入**:已有记忆索引 + 本轮记录(note 不计,工具调用只留名字与参数摘要,工具结果截断到 300 字,总长保留最后 12000 字)。交互会话侧重用户的纠正、偏好、规则与理由;无人值守侧重执行中的经验(有效做法、踩过的坑、环境与数据源事实)。"宁可不记"(Codex)。
- **写入**:只有 create 与 append。create 的文件名已存在,或与已有记忆的名称 + 描述相似度 ≥0.6 → 改为 append 到那条;append 的目标不存在 → 丢弃;文件名不合法 → 丢弃。每轮最多 3 条。**不改写、不删除**。
- **模型**:与召回选择相同(`model_tiers.fast` 属于当前 provider 时用它,否则当前模型)。失败只记日志,不影响对话。

## 不变量

- 提取在后台任务里执行(登记在 harness 的后台任务集合),不阻塞回复。
- 只追加信息,不破坏已有记忆;整理与删除留给做梦整理(未实现,见设计文档 §3.2)。
