---
title: 记忆:强制注入的索引 + 每轮按语义召回
status: active
date: 2026-10-19
---

# 记忆:强制注入的索引 + 每轮按语义召回

## 1. 现状与问题(2026-10-19)

**现在有的**

- 强制注入:项目根目录 `AGENTS.md`(`project_instructions` note)、当天日期。
- 自动召回:`SearchMemoryInjector`(`memory/markdown.py`),每条用户消息用原话搜记忆树,取前 3 条,只给"标题 — 描述"(每条 ≤120 字,共 ≤600 字),以 `memory_recall` note 附在消息后;同一文件一个会话只注入一次,已 Read 的跳过,会话累计 60KB 停止,检索词少于 2 个不召回。
- 写入:`memory_write`(create / refine / supersede,查重门禁)只写 `L2/rules/*.md`;`memory_search` 搜整个记忆树。

**问题**

1. **模型不知道有记忆机制**。系统提示词没有记忆说明;demo 里用户问"你的记忆有几层",模型只能从 `memory_write` 的工具描述猜("L2 规则层 + L1 场景记忆")。
2. **没有强制注入的记忆索引**。模型不知道有哪些记忆,除非碰巧被召回或自己去翻;记忆入口 `README_ai.md` 不注入。
3. **召回是字面重叠**。按两字片段重叠打分:换个说法就搜不到(问"选股偏好"不一定命中写着"量价齐升"的那条);那次"记忆有几层"什么都没召回。
4. **召回范围是整个项目目录**。`AGENT.md`、`SKILL.md`、代码仓库里的文档都参与打分,混进噪声。
5. **只给标题和描述**,真正的内容要模型再 Read;模型常常不读。
6. **记忆没有类型**,位置不统一:default 项目在 `memory/*.md`(用 Write 写的),demo 在 `L2/rules/*.md`(`memory_write` 写的,`status: candidate`);`L1 / L2` 是代码内部的层次名,暴露给了模型。

## 2. 参考:Claude Code 的记忆(auto memory)

- **位置与格式**:每个项目一个记忆目录;一条记忆一个文件,frontmatter `name / description / type`;`MEMORY.md` 是索引,每条一行"标题 — 一句话钩子",限 200 行。
- **类型**:`user`(用户是谁、偏好、专长)、`feedback`(用户对做法的纠正或认可,附原因与适用场景)、`project`(进行中的工作、目标、约束,相对日期转绝对日期)、`reference`(外部资源的位置)。不存代码结构、git 历史等能从仓库直接得到的东西。
- **强制注入**:索引每个会话都在上下文里。
- **按需召回**(`memdir/findRelevantMemories.ts`):每条用户消息用一个快速模型,根据"用户这句话 + 所有记忆的文件名和描述"挑出**确定有用**的最多 5 条(`max_tokens` 256,索引里已有的不重复挑),把这几条的内容作为提醒附上;记忆超过 1 天会附"这条记忆写于 N 天前,可能过时,使用前核实"。
- **系统提示词里有一段记忆说明**:记忆放在哪、各类型何时存、怎么存(写文件 + 更新索引)、不要存什么、什么时候该读、记忆可能过时要核实。

## 3. 设计

### 3.1 存储:项目根目录下的 `memory/`

- 一条记忆一个文件 `memory/<slug>.md`,frontmatter:

  ```markdown
  ---
  name: 选股偏好
  description: 偏好量价齐升、困境反转;推荐时按想象空间 / 兑现程度 / 翻倍概率排序
  type: user | feedback | project | reference
  pinned: false        # 可选,true = 全文每个会话强制注入(短而关键的规则)
  updated: 2026-10-19
  ---
  正文(feedback / project 类附"原因"与"怎么用")
  ```

- 记忆文件是唯一权威。**索引不落盘**,由记忆文件的 frontmatter 实时生成(可重建的视图,不做双向同步)。
- 召回只看 `memory/`;项目里的其他文档(代码仓库的 doc、`AGENT.md`、`SKILL.md`)不再算记忆,仍可用 `memory_search` 检索(见 3.6)。

### 3.2 强制注入

| note | 内容 | 时机 | 上限 |
|---|---|---|---|
| `memory_index` | 全部记忆的索引:每条一行 `- [类型] 名称(memory/xxx.md)— 描述`,按类型分组 | 会话开始注入;记忆增删改后下一条消息重新注入(变化才注入) | 200 行 / 8000 字,超出截断并提示用 `memory_search` |
| `memory_pinned` | `pinned: true` 的记忆全文 | 同上 | 共 2000 字,超出截断 |

与环境段同一条件:完全替换提示词且不放 `${env}` / `${base_prompt}` 的 agent、没有 memory 工具组的 agent 不注入。

### 3.3 每轮语义召回(照 CC findRelevantMemories)

- 每条用户消息(检索词 ≥ 2 个、记忆数 ≥ 1)调用一次选择模型:系统提示词译自 CC 的 `SELECT_MEMORIES_SYSTEM_PROMPT`(中文),输入"用户这句话 + 索引(文件名、类型、描述)",输出**确定有用**的最多 5 个文件名;关闭思考,`max_tokens` 256。
- 排除:已强制注入全文的 pinned 记忆、本会话已召回过的、模型已 Read 过的。
- 选中的记忆**附全文**:每条 ≤2000 字,本次合计 ≤6000 字,会话累计 60KB 后停止(沿用现有上限)。超过 1 天的附"这条记忆写于 N 天前,可能已过时;涉及具体文件、数字、状态的,使用前核实"。
- 选择模型:`model_tiers.fast` 已配置且属于当前 provider 时用它,否则用当前会话的模型(关闭思考)。超时(`memory.select_timeout_s`,默认 3 秒)或出错时退回字面检索(只在 `memory/` 内,名称与描述加权),并记日志。
- 代价:每条消息多一次小的模型调用(输入约 = 索引长度,输出 ≤256 token),首个回复延迟增加约 1–3 秒。配置 `memory.recall`:`select`(默认,照 CC)/ `lexical`(只用字面检索,不调模型)/ `off`。

### 3.4 写入:`memory_write`

- 动作:`create`(name、description、type、正文必填;先查重,与已有记忆的名称 / 描述高度相似时拒绝并提示用 update)、`update`(改正文或 frontmatter)、`delete`(删除错误或过时的记忆)。
- 去掉 `refine / supersede` 与 `status: candidate / deprecated` 链:CC 的做法是直接更新或删除,替代链没有被用到,反而让模型多一层概念。
- `memory/` 目录对 Write / Edit 受保护(同现在的 `L2/`),`memory_write` 是唯一写入口,保证 frontmatter 完整、索引正确。人手工编辑文件不受影响(加载时容错:缺 description 取正文第一句,缺 type 记为 project)。

### 3.5 系统提示词加"记忆"一段

译自 CC auto memory 段并压缩(中文,约 600 字),只在 agent 有 memory 工具组时加入:

- 记忆是什么、在哪:本项目的 `memory/`,跨会话保留。
- 你会自动看到:会话开始时的记忆索引与置顶记忆;每条消息后可能附上与之相关的记忆。
- 四种类型各自什么时候存(用户纠正做法或认可了非显而易见的做法 → feedback;了解到用户身份 / 偏好 → user;项目进展、目标、约束 → project;外部资源位置 → reference);相对日期写成绝对日期。
- 不存:能从文件或工具直接查到的东西、只对本次对话有用的东西;已有的就更新,不重复建;发现记忆错了就更新或删除。
- 记忆可能过时:按记忆行动前,涉及具体文件、数字、状态的先核实。
- 用户问"你记得什么 / 记忆怎么运作"时,按上面如实回答。

### 3.6 其他

- `memory_search` 保留,描述改为中文:"检索本项目的记忆与文档"(范围仍是整个项目目录),用于索引里没有、需要翻项目文档的场景。
- `README_ai.md` 不再是记忆入口:新项目不再生成它;已有的文件保留不动(用户可能写了内容),只是不参与记忆。
- `L1 / L2` 字样从工具描述与提示词中去掉。

## 4. 迁移(一次性,`xana sync` 时执行,先备份)

| 项目 | 现有 | 迁到 |
|---|---|---|
| default | `workspace/memory/user_stock_picking_preferences.md`(title / status / updated) | `memory/user_stock_picking_preferences.md`:`name` = title,`type: user`,`description` 由迁移脚本取正文第一句、我人工校对后写入 |
| demo | `workspace/L2/rules/pattern_dual_surge_v2.md` | `memory/pattern_dual_surge_v2.md`,`type: project` |
| demo | `workspace/L2/rules/project_workflow_research.md` | `memory/project_workflow_research.md`,`type: project` |
| demo | `workspace/L2/rules/terms_fundamentals_semantics.md` | `memory/terms_fundamentals_semantics.md`,`type: feedback`,`pinned: true`(术语口径,短而关键) |

- 迁移后删除空的 `L2/` 目录;旧字段 `status / evidence / supersedes / created` 去掉(内容不变)。迁移前整体备份到 `~/.xana-memory-backup-<时间>/`。
- 代码侧删除:`L2/` 的写保护与路径、`refine / supersede` 分支、`SearchMemoryInjector` 的整树召回(召回改为只看 `memory/`)、`ENTRY_DESC_CHARS` 等只给条目的逻辑(字面兜底仍用)。
- 需同步更新的文档(实施后列清单确认):`doc_ai/memory.md`、`memory_injector.md`、`memory_rules_gate.md`(改写为记忆写入)、`memory_search_engine.md`、`harness_prompt_assembly.md`、`tools.md`、`project` 相关(新项目不再生成 `README_ai.md`)。

## 5. 实现(2026-10-19)

- `memory/store.py` `ProjectMemoryStore`:解析、读写、索引 / 置顶渲染、字面检索、查重;`migrate_legacy`。
- `memory/recall.py` `ProjectMemoryInjector`:唯一的 MemoryInjector,块的 `placement` = index / pinned / recall。
- `runtime/memory_selector.py` `ModelMemorySelector`:选择提示词(译自 CC)与结果解析;boot 组装后注入。
- harness:按 placement 生成 `memory_index` / `memory_pinned` / `memory_recall` 三种 note;agent 有 memory 组时系统提示词加"记忆"段。
- `memory_write` 改为 create / update / delete;`memory/` 对 Write / Edit 受保护。
- 原 L2 规则门禁(查重 + supersede 链)只剩 evolution 的变更单在用,原样移到 `evolution/change_gate.py`,行为不变。
- 删除:`SearchMemoryInjector`、`memory_entry`、新项目的 `README_ai.md` 种子。
- 迁移在 `xana sync` 时对所有项目执行(`cli/commands/sync.py` `_migrate_memories`),备份到 `~/.xana-memory-backup/<时间>/`。

## 6. 不做

- 不做向量检索:记忆条数在几十到几百的量级,让模型读索引挑选比向量相似度更准,也不用维护嵌入。
- 不让模型直接编辑索引:索引由记忆文件生成,避免两处不一致。
- 不自动从对话里提取记忆(CC 有后台"记忆整理"任务):先把"模型主动存 + 每轮召回"做扎实,之后再评估。

## 7. 验证

- 单测:索引生成与截断、pinned 注入与上限、变化才重新注入;选择模型的输入输出解析、排除规则、全文上限、过时提示、超时退回字面检索、`recall=off/lexical`;`memory_write` 三动作与查重、`memory/` 写保护;迁移脚本(幂等、备份、字段映射)。
- 端到端:真实 boot 下 chat 的第一轮请求含记忆段、索引 note;换说法提问能召回到对应记忆(如问"选股偏好"召回"量价齐升"那条)。
- 线上:迁移后在 default / demo 里问"你记得我什么""记忆怎么运作""帮我看看 X(会用到某条记忆的问题)",看回答与召回记录。
- 全量测试 + compact 回归网。
