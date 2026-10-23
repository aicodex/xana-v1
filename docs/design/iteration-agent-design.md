---
title: 迭代智能体:目标契约 → 可仿真的工具环境 → 固定迭代流程(以 demo 业绩预估为试验场)
status: accepted
date: 2026-10-24
---

# 迭代智能体:目标契约 → 可仿真的工具环境 → 固定迭代流程

## 1. 目标与范围

做一个**通用的迭代智能体**(xana 核心,内置 `iterator`):在每日预算内,按固定流程反复"跑 → 评 → 析 → 改 → 验"另一个 agent,让它越来越接近人给它定的目标。

用 demo 项目里的**业绩预估智能体**(`earnings-estimator`,业务层,不进核心)当试验场。它跑出来的问题同时推动三样东西改进:

| 被改进的 | 归属 | 怎么改 |
|---|---|---|
| 业绩预估智能体的工作流 | demo | 迭代智能体自动改(仅正文与经验条目,§4) |
| 搜索(日期范围、来源、泄漏) | 核心(xana-search) | 迭代智能体出"搜索问题单",人改,改完回归 |
| 迭代智能体自身 | 核心 | 看它的账本(改进是否在留出集成立、花了多少运行、归因是否经反事实验证),人改它的提示词与工具 |

分层原则:**核心不出现任何 A 股 / 财报概念**;demo 只经核心开放的接口(评测套件约定 §8、派生工具 §5.2)接入。

## 2. 参考(2026-10-24 调研)

| 工作 | 做法 | 采纳 |
|---|---|---|
| [GEPA](https://arxiv.org/abs/2507.19457)(ICLR 2026 Oral) | 读轨迹(推理 / 工具调用 / 工具输出)做自然语言反思改提示词;保留各样本最优版本组成的 Pareto 前沿;比 GRPO 少用至多 35 倍 rollout | 以读轨迹改工作流为主,不训练权重;候选按样本保留前沿 |
| [ACE](https://arxiv.org/abs/2510.04618)(ICLR 2026) | 生成 / 反思 / 整理三角色,经验写成增量 playbook 条目,避免越改越短、越改越空 | 经验条目 `playbook.md` 增量维护(§4) |
| [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)、[ADAS](https://arxiv.org/abs/2408.08435)(ICLR 2025)、[AFlow](https://arxiv.org/abs/2410.10762)(ICLR 2025 Oral) | 历代版本档案,从中取样变异,以实验结果而非证明判定 | 小档案(前几名版本);不做开放式大搜索(太贵) |
| [HarnessFix](https://arxiv.org/abs/2606.06324)(2026) | 轨迹归一化为与 harness 部件对齐的中间表示,定位出错步骤 → 修复 → 回归验证 | trace 对齐到"哪一步 / 哪条工作流规则 / 哪个工具";修改必须过回归 |
| [Who&When](https://arxiv.org/abs/2505.00212)(ICML 2025 Spotlight) | 失败归因:定位出错 agent 53.5%,定位出错步骤仅 14.2% | 读轨迹归因不可靠 → 关键归因用分叉重跑验证(§7.3) |
| [Halawi et al.](https://proceedings.neurips.cc/paper_files/paper/2024/hash/5a5acfd0876c940d81619c1dc60e7748-Abstract-Conference.html)(NeurIPS 2024) | 检索按日期截止;测试题只取模型知识截止之后 | 回测同时截断检索与模型记忆(§11.5) |
| [Lookahead Bias](https://arxiv.org/abs/2512.23847)(2025) | 只给"公司 + 日期"探测模型是否记得结果;股票收益预测约 37% 的效果被记忆放大 | 回测样本先做记忆探测 |
| [ToolEmu](https://proceedings.iclr.cc/paper_files/paper/2024/hash/7274ed909a312d4d869cc328ad1c5f04-Abstract-Conference.html)(ICLR 2024) | 大模型模拟工具执行做风险测试;人工核验 68.8% 的模拟失败在真实世界成立 | 模型模拟只用于探索 / 工具原型,**不用于评分**(§6.2) |
| [FutureSim](https://arxiv.org/abs/2605.15188)(2026) | 按时间顺序回放 2026-01~03 真实新闻,问题到期揭晓 | 真实数据 + 时间截断,而非编造数据 |
| [Kim, Muhn & Nikolaev 2024](https://www.semanticscholar.org/paper/Financial-Statement-Analysis-with-Large-Language-Kim-Muhn/77f2e8dc258600dea936c8356770dd0bdc478252) | 仅凭标准化财报判断盈利方向,准确率超过分析师 | 方向类指标作主指标(§11.4) |

## 3. 总体结构

```
iterator(内置,固定流程,通用提示词)
  0 环境可信性检查 ── 不过即停,报告缺什么
  1 读目标契约 + 工作流 + 经验条目 + 账本
  2 看最近表现(上一版结果表、失败样本)
  3 跑:候选版本 × 样本,隔离会话,工具经环境层(录制回放 / 时点约束 / 禁止)
  4 评:评测套件的打分命令(确定性)
  5 析:读 trace 归因 → 分叉重跑验证 → 按归因分流
  6 改:正文 / 经验条目(自动);目标、工具、搜索、数据(提议 / 问题单)
  7 验:训练集 + 留出集,不退化才保留,写账本
被迭代 agent(如 demo/earnings-estimator):目标契约(人) + 工作流(可迭代) + 派生工具
基础设施(核心):trace 录制 · 工具环境层 · 派生工具注册 · 评测会话 · 每日预算
```

## 4. Agent 目标契约

现状:AGENT.md 必填的 `goal` 只是一句描述,载入后成为 `manifest.description`(委派列表用);正文即系统提示词(工作流)。两者没有归属之分,迭代者若能改目标就能"改题刷分"。

改为三部分,归属不同:

| 部分 | 内容 | 谁能改 |
|---|---|---|
| **目标契约**(frontmatter) | `goal` 做什么事(一两句);`success` 怎么算做好(指标 + 阈值,指向评测套件);`constraints` 硬约束(不用未来信息、数据纪律、预算);`non_goals`(可选);`tools`;`eval`(评测套件名);`allow_exec`(§5.4) | **只有人** |
| **工作流**(正文) | 步骤、方法、输出格式 | 迭代者,有版本,可回滚 |
| **经验条目**(同目录 `playbook.md`) | ACE 式增量条目,每条注明来源 episode 与样本 | 迭代者增删 |

```yaml
---
kind: agent
name: earnings-estimator
goal: 在给定时点粗略估计某 A 股公司尚未披露的单季归母净利润(数值、环比 / 同比方向、盈亏)。
success:
  eval: earnings-q            # .xana/evals/earnings-q
  metrics:
    qoq_direction_acc: ">= 0.75"
    profit_sign_errors: "== 0"
    scaled_error_median: "< baseline:consensus"
constraints:
  - 只使用研究时点之前已公开的信息
  - 数据统一经 qt 工具取,不读原始数据文件
non_goals:
  - 不给投资建议、不做估值
tools: [qt, web, Read]
---
(工作流正文……)
```

数据结构:`AgentSpec` 增加一个目标契约对象(`goal / success / constraints / non_goals / eval / allow_exec`),`description` 仍由 `goal` 生成。新字段均可选,现有 AGENT.md 无需迁移;**没有 `success` 与 `eval` 的 agent 不能被迭代**(环境检查直接报缺什么)。

保证迭代者只改该改的:它修改 agent **只能经版本工具 `agent_version`**(§9.2),该工具只接受正文与 `playbook.md` 的修改,frontmatter 有任何变化即拒绝并转成"目标修改提议";`protected_dir` 本来就拦它直接写 AGENT.md。

## 5. 工具

### 5.1 声明字段(ToolSpec)

现有 `read_only / destructive / open_world`,新增:

- `temporal`:`static`(结果不随时间变)/ `as_of`(支持按时点约束)/ `live`(只能取当前状态,回测时只能回放或禁止)。
- `side_effects`:是否改变外部世界(评测时默认禁止或模拟)。实施时核对能否直接复用 `destructive` 的语义,能则不新增。

所有内置工具补声明。

### 5.2 派生工具(TOOL.md)

给 agent 一项执行能力,不给 Bash,而是写声明得到一个有类型的工具:

```yaml
---
kind: tool
name: qt
description: A 股行情与财务数据(本地,只读)
command: ["./qt", "--format", "json"]      # 固定前缀,按参数数组执行,不经 shell
cwd: project
subcommands:                                # 每个子命令一份参数 schema
  income: {args: {code: {type: string}, quarters: {type: integer, default: 12}}}
  fcst:   {args: {code: {type: string}}}
temporal: as_of                             # 必填
as_of: {arg: "--as-of"}                     # 由评测环境注入,不在模型可填的参数里
side_effects: none                          # 必填
timeout_s: 60
---
(给模型看的使用说明)
```

- 由核心的通用命令工具执行:参数按 schema 校验后拼成参数数组,**不经 shell**(无注入,也无法顺手跑别的命令)。模型给的字符串不能以 `-` 开头(防止冒充 `--as-of` 等参数;声明的 enum 值除外),时点参数追加在最后。
- 子命令可单独写 `temporal` / `as_of` 覆盖工具级声明(实测 `qt` 有子命令不支持时点)。
- 派生工具属于默认组合里的 `derived` 组:委派时子 agent = 自身选择 ∩ 调用方工具,若不在默认组合,选 `default` 的 chat 委派时会把它截掉(2026-10-24 实测 equity-researcher 因此退回网页搜索)。
- `side_effects` 复用 `destructive`(此前没有代码读它,只是声明);补齐了 cron / notify_user / agent_define / project_manage。
- 复杂工具以后走 MCP(配置已有 `mcp.servers`,boot 未实现);TOOL.md 覆盖"把现有命令行包成工具"这一最常见情况,两者并存。

### 5.3 注册与创建(与 agent 对称)

| | agent | 工具 |
|---|---|---|
| 文件 | `.xana/agents/<名>/AGENT.md` | `.xana/tools/<名>/TOOL.md` |
| 两级 | `~/.xana/agents`、项目级 | `~/.xana/tools`、项目级 |
| 注册 | `FileAgentRegistry`,热加载 | `FileToolRegistry`,热加载,并入工具目录 |
| 定义 | `agent_define`(catalog / create / delete) | `tool_define`(catalog / **test** / create / delete) |
| 技能 | `agent_creator` | `tool_creator` |

`tool_creator` 流程:
1. 弄清能力、对应命令、哪些参数允许模型填。
2. 起草 TOOL.md(`temporal`、`side_effects` 必填)。
3. `tool_define(action="test", 示例参数)` 真实试跑并展示输出;声明 `temporal=as_of` 的再带一个过去日期试一次,确认确实截断。
4. 用户确认后 `tool_define(action="create")`,热加载即用。
5. 告诉用户哪些 agent 可以把它加进 `tools`。

谁能调 `tool_define`:`chat` 在用户确认后注册;`iterator` 能起草、试跑,**不能注册**,注册走"派生工具提议"(无人值守时经 `notify_user` 的 decision 流程)。

### 5.4 Bash 规则

| agent | Bash |
|---|---|
| `chat`(内置) | 允许 |
| `iterator`(内置) | 允许,但有护栏(§9.3) |
| 其他 | **默认禁止**;仅当用户明确开启 `allow_exec: true`(须写原因) |

- `allow_exec` **模型写不进**:`agent_define` 遇到即拒绝;`agent_version` 不能改 frontmatter;只能用户手改文件或发斜杠命令 `/agent allow-exec <name> <原因>`(斜杠命令只有用户能发)。
- 代码强制:工具解析时非 `chat` / `iterator` 且未开 `allow_exec` 的 agent 去掉 `exec` 组与 Bash(`default` 组合对它们不含 exec);`agent_define create` 拒绝 exec / Bash 并提示改用派生工具;热加载遇到已有 AGENT.md 写了 exec 记警告并去掉(同现有未知工具处理)。
- `allow_exec` 的 agent 在评测时 Bash 仍按环境策略处理(沙箱 / 回放 / 禁止),环境检查把它标为"可信度降低"。
- `agent_creator` 增加"能力分解"一步:需要执行能力 → 转 `tool_creator` 派生工具;用户坚持要 Bash → 告知用 `/agent allow-exec` 自己开启及其代价。

## 6. 工具环境层

### 6.1 为什么是核心环节

迭代 = 同一道题换做法再做一遍、比较好坏。比较要有意义需要:
1. **回到过去**:信息只能到当时为止(预测、决策、投研类任务都是)。
2. **可重复**:真实搜索每天在变,新旧版本拿到的信息不同就分不清是改好了还是碰巧搜得好;小预算下噪声会淹没改进。
3. **反事实**:"第 k 步换个做法会怎样"要求前 k 步原样重放(§2 Who&When)。
4. **副作用**:发消息、下单、改文件不能在迭代里真执行。

### 6.2 策略

每个工具在评测会话里按环境配置选一种:

| 策略 | 做法 | 可信度 | 用途 |
|---|---|---|---|
| `replay` | 录制里有相同调用 → 回放 | 最高(真实数据) | A/B、分叉重跑、省钱 |
| `live+约束` | 真实执行,注入时点 / 环境变量 | 高(取决于源的日期可靠性) | 新版本发起录制里没有的调用 |
| `simulate` | 规则脚本或模型模拟 | 中 / 低 | 副作用工具;**还不存在的工具做原型** |
| `deny` | 拒绝 | — | 不该在评测里出现的工具 |
| `live` | 原样执行 | — | 非评测会话(默认) |

硬规则:**信息类工具在评分时不能用模型模拟**(模型会把记住的结果混进去,等于喂未来信息,且 agent 会被优化成适应模拟器)。

回放缺失时按配置回退到 `live+约束` 或 `simulate`,**计数**;回退越多该样本分数越不可信,打分结果标注回退次数。

### 6.3 接入

- 现有 `ToolLifecycleContributor` 只能观察,不能替换结果。在 `_dispatch_tool` 增加可接管执行的接入点,**新增 ABC** `ToolEnvironment.execute(调用, 工具, 上下文, 真实执行) → 结果`(用户 2026-10-24 同意)。实现时放在 `tools/interfaces.py`(与 `Tool` 同处):它由 `boot()` 构造注入 `DefaultHarness`,不是扩展贡献者。首个实现 `tools/environment.py::TapeToolEnvironment`。
- 每次 run 的环境是数据 `ToolEnvConfig(as_of, tape, strategies, on_miss)`,经 `request.metadata["tool_env"]` 进入 harness、放进 `ToolContext.env`;`AgentRunner.run(tool_env=...)` 让 `agent_run` / `deep_research` 的子 run 原样继承。工具按调用给出时间声明 `Tool.temporal_for(调用)`(缺省 = `spec().temporal`;TOOL.md 子命令可覆盖)。
- 所有工具都经同一入口,在此一处实现录制 / 回放 / 拒绝 / 回退计数,**不让每个工具继承 hook**(工具只做 §5.1 声明)。
- `ToolContext` 增加环境信息(`as_of` 等):支持时点约束的工具自己读它(搜索自动带日期上限、派生工具按 `as_of.arg` 注入);不支持的由环境层按声明回放或禁止。
- 子 agent 的运行继承父运行的环境(`agent_run` / `deep_research` 经 HarnessContext 下发)。
- 评测会话的"今天日期"注入改为 `as_of`。

## 7. Trace 日志

### 7.1 现状

`sessions/runs/*.jsonl` 每条 `ts` 为 `0.0`;无 token 用量、耗时、系统提示词版本;子 agent 过程不在同一棵树上。不够 debug,也无法回放。

### 7.2 补齐(同一份数据既供 debug 也是回放的"磁带")

- 每步:时间戳与耗时;模型调用的输入 / 输出 / 缓存 token、停止原因;思考内容;工具完整输入输出(大输出存旁路文件,行内存引用)、错误。
- 版本:系统提示词与 AGENT.md(正文 + playbook)的哈希。
- 调用树:父子 run 关系。
- 环境:每次工具调用走了哪种策略、是否回退。

### 7.3 给迭代者用的工具

- `trace_view(run, 步骤范围, 过滤)`:先给摘要(步骤列表、工具调用、错误),再按需展开,不把整份 trace 塞进上下文。
- 分叉重跑:从第 k 步分叉(前缀照录制回放),注入一句提示或换一版工作流,看结论是否改变。在 `agent_run` 的 `resume` 基础上增加分叉。

## 8. 评测套件约定(核心定格式,项目填内容)

```
<项目>/.xana/evals/<名>/
  suite.yaml        目标 agent、拆分、单样本预算、环境配置、打分命令、泄漏审计规则
  cases.jsonl       一行一个样本:{id, split, input, as_of?, ...}(input 原样交给被测 agent)
  scorer            打分命令(脚本),只读
  tapes/            录制(由核心写入)
```

- `split`:`train`(迭代时用)/ `heldout`(只用于验收,分数与样本细节**不回流**给迭代者的分析)/ `live`(真实未来,到期后才有真值)。
- 打分命令:标准输入为 `{case, prediction}` 列表,标准输出为每样本 `{score, big_miss: bool, miss_type?, detail}` 与汇总指标;**确定性,不用模型打分**。
- 泄漏审计规则:trace 中不允许出现的模式(如答案数值、"已公告"类事后表述、被禁止的工具调用),命中则样本作废。
- 环境配置:按工具名或按 `temporal` / `side_effects` 声明选策略(§6.2)。

迭代者只认这个约定,不懂业务。

## 9. 迭代智能体

### 9.1 固定流程(提示词通用)

| 步 | 内容 |
|---|---|
| 0 环境可信性检查 | 目标契约齐全(`success` / `eval`);列出被测 agent 的工具,逐个核对 `temporal` / `side_effects` 有合理策略;被测 agent 有 exec 或 `allow_exec` 时标"可信度降低";抽样对比回放与真实执行;跑一遍泄漏审计。不过 → 停,报告缺什么 |
| 1 读 | 目标契约、工作流、经验条目、账本(以往假设与判决) |
| 2 看最近表现 | 上一版结果表、大偏差样本 |
| 3 跑 | 候选版本 × 训练样本,隔离会话,并行,受预算约束 |
| 4 评 | `eval_score`(调套件的打分命令) |
| 5 析 | 对大偏差读 trace 归因;关键归因做分叉重跑验证;按归因分流(§9.4) |
| 6 改 | 正文 / 经验条目经 `agent_version` 提交新版本;其余出提议或问题单 |
| 7 验 | 训练集 + 留出集;留出集指标优于当前版本超出噪声、且不破坏原先做对的样本才保留;写账本(假设、改动、证据、判决) |

### 9.2 工具

`eval_run`(候选版本 × 样本,经环境层)、`eval_score`、`trace_view`、分叉重跑、`agent_version`(只改正文 / playbook;提交、比较、回滚)、`tool_define`(起草 / 试跑)、提议与问题单(经 `notify_user`)、账本读写(只追加)。

### 9.3 护栏

迭代者有 Bash,就有能力改评分脚本、真值、样本来刷分(自我改进类工作最典型的失败):

- 评测套件目录、真值数据、打分命令对它**只读**(`protected_dir`)。
- 分数只认 `eval_score` 的输出;它在 Bash 里自己算的不算。
- 账本只追加。
- 留出集只给汇总指标,不给样本细节。

### 9.4 归因分流

| 归因 | 去向 |
|---|---|
| 工作流(过信机构、忘了周期属性、没做季度拆分、漏看同行……) | 自动改正文 / 经验条目,留出集验证 |
| 该搜到没搜到;搜到截止日之后的内容 | 搜索问题单(查询、日期范围、期望找到的内容),人改 xana-search 后对同批样本回归 |
| 缺数据 / 缺工具 | 派生工具提议(TOOL.md 草稿 + 理由 + 关联样本) |
| 不可预测(突发一次性事件) | 标记,不计入"该改进" |
| 目标或指标本身有问题 | 目标修改提议(人决定) |

### 9.5 账本与评估迭代者本身

复用 `evolution/ledger.py`(只追加)与 `EpisodeRunner`(软 / 硬预算阈值、landing 期拒绝新运行、canary、保留 / 回滚)。迭代者自身的评估从账本算:被保留改动在留出集的真实增益;每次有效改进的运行次数;归因经分叉重跑验证的比例。

## 10. 搜索:日期范围

现状:xana-search 只有 SearXNG `time_range`(day / week / month / year,相对)。

改动(在 xana-search):
- 请求增加 `start_date / end_date`。
- 引擎原生参数尽量用上(Google `before:` / `after:` 运算符、Bing / 百度时间参数)。
- 严格事后过滤:按结果发布日期过滤;缺日期时用正文抽取已有的 `published` 补;严格模式丢弃无法确认日期的页面。
- 公告走官方源:接巨潮资讯公告查询(支持日期范围),公告与财报原文从这里取。
- 黑名单:行情页、F10 等显示最新数据、发布时间不可信的页面。
- 核心 `web_research` / `deep_research` 读 `ToolContext` 的 `as_of` 自动设 `end_date`。

测试:5–10 家公司,按"截止日前的业绩预告 / 经营公告 / 行业新闻"查询,统计**泄漏率**(截止日后结果占比)与**召回率**(截止日前确实存在的关键信息找到多少)。

**实现(2026-10-24,xana-search b504486)**:`start_date / end_date / strict`;google 实例另按 `after: / before:` 搜一次;日期按网页元数据 > 正文日期(摘要模型)> 引擎日期 > URL 日期判定;范围外、黑名单(行情 / F10)、严格模式下无日期 → 剔除,且不留摘要 / 标题 / 全文文件,只计数。xana 侧 `web_search` / `web_open_url` 改为 `temporal=as_of`,模型可给日期范围,评测 run 的 `as_of` 是硬上限并强制严格;`background_research` 仍为 live(评测中拒绝)。

**首次测试**(5 家种子公司,截止日 = 本公司下一份财报前一天,3 条查询,读 8 页):
| | 截止日后来源 | 正文出现未发布财报 | 召回(截止前最后财报的同比数字) |
|---|---|---|---|
| 不过滤 | 世运 2 条(含 1 个行情页) | 胜宏 1 页 | 2 / 5 |
| 过滤 | 0 | 0(日科 1 页是 2025 年的三季报,检测误报) | 1 / 5 |

时点语义:**研究时点 = 当天开盘前**。A 股盘后公告的公告日记为次日(胜宏 2026 半年报 8-27 晚发布,ann_date 8-28),qt 按 ann_date ≤ as_of 截断;网页发布日只到天,所以评测 run 的网页搜索截到 as_of 前一天(实测 end_date=8-27 搜到了 8-27 晚的半年报报道)。

结论:过滤后无泄漏,但严格模式下无日期结果被丢,召回偏低;网页搜索对"某公司某季度业绩"本来召回就低(5 家里 2 家)。补救放在第 4 步:公告与财报走巨潮资讯(日期可靠),作为 demo 的派生工具,网页搜索只作补充。

## 11. demo:业绩预估智能体

### 11.1 数据与工具

本地 `~/quant/research/data` 可按时点截断:利润表(`f_ann_date`)、业绩预告与快报(`ann_date`)、卖方盈利预测 `stock_report_rc`(`report_date`;注意 `quarter=2027Q4` 是**年度**预测)、披露日历(预约 / 实际日)、主营构成、板块成分(找同行)。

demo 侧补充(不改核心):
- `qt` 派生为 TOOL.md 工具,`temporal=as_of`;`QT_AS_OF` 作为强制上限(忘带 `--as-of` 也读不到未来)。
- `qt peers`:同板块同行在某时点前的披露与预告。
- `qt quarter`:累计数推单季。
- 一致预期拆季度:年度一致预期 − 已实现累计。
- 打分脚本:从本地数据算单季归母净利真值。

### 11.2 工作流 v0(正文,可迭代)

1. 确定时点与目标季度;公司档案(主营构成、行业、是否周期股)。
2. 历史:近 8–12 季单季营收、毛利率、归母与扣非净利;季节性;一次性损益。
3. 自身信号:时点前已发的预告 / 快报(有则直接用并标注);订单、产能、减值类公告。
4. 机构预期:年度预期拆季度;机构分歧与预测时效。
5. 同行与产业链:同板块已披露 / 预告的同行;上下游价格与销量;新闻验证。
6. 输出 JSON:点估计、区间、环比与同比方向、盈亏、置信度、关键假设、每条证据的日期。

### 11.3 按时点构造样本

| 时点 | 例(as_of → 目标季) | 可用信息 | 主要考察 |
|---|---|---|---|
| A 本季报刚出 | 05-05 → Q2;09-05 → Q3 | 刚出季报、随后修正的研报 | 外推、季节性、机构预期拆季度 |
| B 季末已过、本公司未出 | 07-20 → Q2;10-15 → Q3 | **同行已发预告 / 快报**、行业月度数据 | 同行与产业链交叉验证(本公司已发预告的样本排除) |
| C 季中 | 08-15 → Q3 | 部分月度数据、商品价格 | 经营推断 |
| D 年报季 | 01-20 → 上年 Q4 | 强制预告(1 月 31 日前) | Q4 减值与年末调整 |

- **实时集**:2026-10-24 起三季报密集披露(披露日历中 2026Q3 预约 5581 条),预测 10-28 之后才披露的公司,一周内可核对,答案不可能在模型训练数据里。
- **回测集**:2026 Q1 / Q2 各时点;先做记忆探测(§2 Lookahead Bias),模型记得的样本剔除。
- 公司覆盖:周期(有色 / 化工 / 养殖)、成长科技(PCB / 光模块)、消费、医药、盈亏摇摆的小盘;有一致预期与无一致预期都要有。

### 11.4 指标与大偏差

- 主指标:环比方向准确率、盈亏方向错误数、归一化误差 = |预测 − 实际| / max(|实际|, |去年同季|)(避免利润近 0 时放大)。
- 大偏差:环比方向错且实际环比变动 > 30%;盈亏方向错;归一化误差 > 50%。
- 基线:天真预测(去年同季 × 趋势)、一致预期拆季度。agent 至少要赢一致预期基线。

### 11.5 防泄漏

1. 数据只经 `qt` 工具(按时点截断 + `QT_AS_OF` 强制上限);被测 agent 无 Bash。
2. 搜索按 `as_of` 截止(§10)。
3. 会话"今天日期" = `as_of`。
4. 评测后泄漏审计(答案数值、事后表述),命中作废。
5. 回测样本做记忆探测;以实时集为准绳。

### 11.5.1 实现与第一张结果表(2026-10-24)

demo 侧:`qt` 新增 `quarter`(单季)、`peers`(申万行业时点成员的最新单季 / 预告 / 快报,最近披露的在前)、`consensus`(年度一致预期 − 已披露累计,按去年季度占比拆下一季);`cninfo` 派生工具(巨潮公告列表 / PDF 正文,研究时点当天 09:00 前发布的才可见);`earnings-estimator`(工具只有 qt / cninfo / web_search / web_open_url,没有文件与记忆工具,防止从工作区文件或记忆召回拿到未来信息);套件 `.xana/evals/earnings-q`(36 个样本:训练 12 = 种子 6 家 × A/B 时点,留出 16 = 8 个行业 × A/B,实时 8 = 10-28 后披露的公司),打分 `score.py`(真值 = 首次披露的单季归母净利;基线:上一季、去年同季、一致预期拆季度)。

v0 工作流(批次 `20261024-024519-7acf-v0-baseline`,kimi-for-coding,单样本约 2 分钟、3–7 次模型调用、3–12 万输入 token):

| | 已打分 | 归一化误差中位 | 一致预期基线 | 上一季基线 | 环比方向准确率 | 盈亏方向错 | 大偏差 |
|---|---|---|---|---|---|---|---|
| 全部 | 26 | **0.122** | 0.177 | 0.254 | 0.77 | 2 | 4 |
| 训练 | 10 | 0.229 | 0.72 | 0.752 | 1.00 | 2 | 4 |
| 留出 | 16 | 0.106 | 0.159 | 0.167 | 0.63 | 0 | 0 |
| 实时 | 0(8 个待披露) | | | | | | |

大偏差:日科化学 A/B 预测盈利 0.2 亿、实际亏损 0.228 亿(盈亏方向错);中富电路 A/B 预测 0.25 / 0.37 亿、实际 0.795 亿(低估环比大增)。胜宏科技 Q2 没有跟随一致预期(拆季 25 亿),A 时点预测 16.0 亿、实际 15.68 亿。齐翔腾达 Q3 于 10-24 披露,数据入库后可打分。

### 11.6 种子样本(用户 2026-10-24 指定)

用户参与过、清楚答案和原因的样本,第 4 步先放进 cases。单季归母净利(亿元)由 `./qt income` 的累计值相减得到。

| 公司 | 单季走势 | 考点 | 建议时点 |
|---|---|---|---|
| 日科化学 300214 | 2026 Q1 0.02 → Q2 0.28 → Q3 -0.23(10-23 披露) | 化工周期:Q2 环比大增不能外推 | A:Q2 报后预测 Q3 |
| 齐翔腾达 002408 | Q1 -0.92 → Q2 2.08 → Q3 亏损(用户口径),预披露 10-24 | 同行先行:日科 10-23 已出 Q3 亏损,次日推齐翔 | B:as_of=10-23 晚 / 10-24 早 |
| 麦捷科技 300319 | Q2 1.04 → Q3 1.38 | 业绩可预期:能否预测到增长 | A / B |
| 世运电路 603920 | Q2 0.48 → Q3 2.36 | 同上(环比大增) | A / B |
| 中富电路 300814 | Q2 0.20 → Q3 0.80 | 同上(环比大增) | A / B |
| 胜宏科技 300476 | Q1 12.88 → Q2 15.68,机构预期很高、实际不达预期;Q3 预披露 10-30 | 不盲信机构:主营绑定英伟达 Rubin 节奏,Q2 产能释放无大变化 | Q2 报前(as_of 8 月中);Q3 作实时样本 |

评测用 `kimi-for-coding`(订阅),这批样本不用省 token,可以多版本、多次重复以估计噪声。

## 12. 预算与定时

- 每日预算(token,按模型折算) → `BudgetTracker` 每日上限 + `EpisodeRunner` 每 episode 预算(软阈值进入 landing,硬阈值强制收尾)。
- 估算:一个样本约 5 万–20 万 token(搜索多);一轮"10 样本 × 2 版本"约 200 万–400 万 token。
- 模型:`kimi-for-coding`。它是订阅额度,不按 token 计费,所以预算用 token 表示。
- 初定(粗估,待校准):每日 600 万 token(`max_day_tokens`),约两轮"10 样本 × 2 版本";每个 episode 软阈值 300 万、硬阈值 400 万;单样本 `max_tokens` 30 万。
- 校准:每次模型调用的输入 / 输出 / 缓存命中 token 记在 trace 行上,run 级合计记在 `UsageSnapshot.total_*`(§7)。跑完第一张结果表后,按实际单样本用量重算以上数值。
- 额度接口:Kimi Code 源码(`packages/oauth/src/managed-usage.ts`)用 `GET {base}/usages`(OAuth Bearer)查额度,返回 `limit5h` / `limit7d` / `monthTotal` / `monthCode`,各含 `usedRatio`、`resetAt`。第 6 步接入:运行前查一次,`usedRatio` 超阈值就停;同时用"本日消耗 token / usedRatio 增量"反推额度对应的 token 数。2026-10-24 未实测(磁盘上的 token 已过期,没有为测试去刷新)。
- **实测校准(2026-10-24)**:demo 当天 8.0M token(基线批次 36 个样本 + 一轮迭代约 55 个评测 run;单样本约 6 万,一轮迭代约 3–4M);同期 Kimi 5 小时额度 28% → 65%、月额度 6.6% → 8.1%,折合 5 小时窗口约 15M、月约 400M token。`/usages` 实际字段是 snake_case(`limit_5h` / `limit_month_total`,`used_ratio` / `reset_time`),本订阅没有 7 天额度。用户说不用省 token,`earnings-q` 的 `daily_tokens` 取 15M(约一个 5 小时窗口,天天用满约占月额度 40%);`eval_run` 每个样本开跑前检查当日用量与 Kimi 额度(≥ 90% 停),跳过的样本记 `skipped` 及原因。
- 套件指纹只算样本、打分文件、打分命令与受保护文件:改并发、预算等运行配置不使已有批次无法打分。
- 定时(已建,demo 任务 `d974cd50`):每天 02:30 以 iterator 跑一轮,结果插回创建它的会话。iterator 以无人值守模式按 cron 唤醒,用完当日预算即停;提议与问题单经 `notify_user`。

## 13. 与现有代码的关系

| 现有 | 处理 |
|---|---|
| `evolution/ledger.py`、`evolution/episode.py` | 复用 |
| `evolution/meta.py`(`MetaEvolutionService`,定时唤醒 + 固定四段 prompt) | 由 iterator 取代;iterator 落地后删除(迁移以旧路径删除为完成) |
| `GatewayAgentRunner` 的 episode gate | 保留,iterator 的 `eval_run` 经它 |
| `agent_creator` / `agent_define` | 加能力分解、拒绝 exec / Bash |
| AgentSpec / AGENT.md 解析 | 加目标契约对象 |
| ToolSpec / ToolContext | 加 `temporal`(`side_effects`)/ 环境信息 |
| `sessions/runs/*.jsonl` | 补字段,同时作回放录制 |
| demo `equity-researcher`(Bash 跑 `./qt`) | 派生 `qt` 工具后关 Bash |
| llm-papers `paper-reader`(Bash 跑 `paperlib.py`、`mv`) | 派生 `paperlib` 工具(含 `move`)后关 Bash |

现有两个 agent 的迁移是用户可见变化:先建好派生工具并验证,再关 Bash,不留断档。

## 14. 实施阶段(每步可独立验证)

1. **trace 录制**:补字段、`trace_view`;验证:一次真实运行的 trace 能完整还原每步。
2. **派生工具 + 工具环境层 + Bash 规则**:TOOL.md、`FileToolRegistry`、`tool_define`、`tool_creator`、`ToolEnvironment`(先实现 `live+约束` / `replay` / `deny`,`simulate` 留接口)、迁移两个 agent;验证:同一样本回放两次结果一致,`as_of` 注入生效,非 chat agent 拿不到 Bash。
3. **搜索日期范围**:验证:§10 测试报告(泄漏率、召回率)。
4. **demo 评测套件**:`earnings-estimator`、`qt` 补充、打分、基线、样本;先跑实时集出第一张结果表。
5. **iterator v0**:在 demo 上跑 10 家 × 若干时点,完成至少一轮"析 → 改 → 留出集验证",账本可读。
6. **每日预算与定时循环**。

## 15. 已确认(2026-10-24)

实施进度:第 1–5 步已完成并部署验证(trace;派生工具、工具环境层、Bash 规则、两个 agent 迁移;搜索日期范围;demo 评测套件与第一张结果表;内置 iterator、agent_version、ledger、套件防篡改)。第 6 步:每日 token 上限(按持久化 run 用量)与 Kimi `/usages` 额度检查已实现;`MetaEvolutionService` 已删除(iterator 取代)。
demo 的 `qt` 已补齐时点:全部子命令支持 `--as-of`,`QT_AS_OF` 为硬上限(公告类按 f_ann_date,调研按 surv_date,主营按财报公告,日历隐藏未发生的实际披露日,行情 / 估值 / 美债截到当日)。

用户全部同意 §15 原列六项:
1. 新增 `ToolEnvironment` ABC(§6.3),定义在 `extension_api/interfaces.py`。
2. 目标契约字段按 §4;`allow_exec` 两种开启方式都支持:手改文件,或用户发斜杠命令 `/agent allow-exec`。
3. ToolSpec 新增 `temporal`;`side_effects` 在实施时核对,能复用 `destructive` 就不新增字段。
4. TOOL.md 格式与 `tool_define` 权限按 §5.2–5.3。
5. Bash 三档规则按 §5.4,两个 agent 按 §13 迁移(先派生工具并验证,再关 Bash)。
6. 模型用 `kimi-for-coding`(Kimi k2.8,OAuth 订阅);预算见 §12,先粗定,之后按 API 用量校准。
