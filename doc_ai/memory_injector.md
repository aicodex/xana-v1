---
title: memory injector
type: doc
status: active
---

# memory injector

## 上级模块

- [memory](memory.md) — memory 模块设计
- 设计:[memory-recall-design](../docs/design/memory-recall-design.md)

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `InjectedBlock`(`placement` = index / pinned / recall)、`MemoryRecord`、`MemorySelector`
- [interfaces.py](../src/xana/memory/interfaces.py) — `MemoryInjector`, `MemoryInjectorRegistry`
- [recall.py](../src/xana/memory/recall.py) — `ProjectMemoryInjector`(唯一实现)
- [store.py](../src/xana/memory/store.py) — `ProjectMemoryStore`(`memory/` 的读写、索引 / 置顶渲染、字面检索)
- [memory_selector.py](../src/xana/runtime/memory_selector.py) — `ModelMemorySelector`(选择模型调用,boot 注入)

## 职责

每条用户消息,`ProjectMemoryInjector.select()` 读 `memory/` 并返回:

- **index 块**:全部记忆的索引,按类型分组每条一行(≤200 行 / 8000 字);
- **pinned 块**:置顶记忆全文(≤2000 字);
- **recall 块**:检索词 ≥2 个时,从非置顶记忆里选出确定有用的最多 5 条,附全文(每条 ≤2000 字、合计 ≤6000 字),超过 1 天的带"写于 N 天前,可能过时,使用前先核实"。

选择照 Claude Code `findRelevantMemories`:注入的 `MemorySelector`(`ModelMemorySelector`,关闭思考的一次小调用,提示词译自 CC)按"这句话 + 记忆的文件名、类型、描述"挑选;超时(`memory.select_timeout_s`,默认 3 秒)或出错退回 `ProjectMemoryStore.lexical_rank`(名称与描述加权的两字片段重叠)。`memory.recall`:`select`(默认)/ `lexical` / `off`(只给索引与置顶)。选择模型取 `model_tiers.fast`(属于当前 provider 时),否则当前模型。

harness 按 placement 生成 `memory_index` / `memory_pinned` / `memory_recall` 三种 note(变化才注入、按路径去重,见 [harness_prompt_assembly](harness_prompt_assembly.md))。

## 不变量

- 记忆文件是唯一权威,索引与置顶内容由 frontmatter 实时生成,不落盘。
- 召回只看 `memory/`;项目里其他文档只经 `memory_search` 检索。
- 注入器本身不调模型;模型调用在注入的选择器里(runtime 组装),选择失败不阻断对话。
