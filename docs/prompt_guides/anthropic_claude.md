# Anthropic Claude 官方提示词指南（xana 框架层吸收版）

- 调研日期：2026-09-23
- 方法：直接下载 platform.claude.com 文档的 `.md` 原文逐段阅读；以下全部为官方文档所述，未写数据处即官方未给。

## 来源

- 通用最佳实践：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- Fable 5.1：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1
- Fable 5：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5
- Opus 5.5：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5
- Opus 5：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5
- Sonnet 5：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5
- Effort：https://platform.claude.com/docs/en/build-with-claude/effort
- Task budgets：https://platform.claude.com/docs/en/build-with-claude/task-budgets
- 中途 system 消息/工具变更：https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
- What's new：https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1 、https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5 、https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5
- 选型：https://platform.claude.com/docs/en/about-claude/models/choosing-a-model 、https://platform.claude.com/docs/en/models/overview
- 工程博客（背景，未逐字引用）：https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents 、https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

## 覆盖范围

- 找到：Fable 5.1 / Fable 5 / Opus 5.5 / Opus 5 / Sonnet 5 各自的专属提示词页 + 通用页 + effort/task budget/中途 system 消息等 API 文档。
- 没找到：统一的“Claude 5 best practices”单页（官方是“通用页 + 各模型差异页”结构）；Sonnet 5 无 Opus/Fable 那样的长程 agent 专节；Cookbook 本次只确认了 crop 工具与 context engineering（memory/compaction/tool clearing）两篇存在，未深读。
- 已在记忆中覆盖、本文不重复：回合续跑/无人值守结束指令、进度更新 thinking 块与无进度提醒、广泛探索、时间预算、聊天思考控制、pasted_content、视觉、前端禁用样式。

## 三个模型的官方差异

| 项 | Fable 5.1 | Opus 5.5 | Sonnet 5 |
|---|---|---|---|
| 定位 | 最强；数小时 agent、深度研究 | 官方“多数负载从这里开始” | 日常编码/agent，便宜 |
| effort 默认 | high | **medium**（Opus 5 为 high） | high |
| thinking | 始终自适应 | 始终开，禁用返回 400 | 默认自适应，可 disabled |
| 强制 tool_choice(any/tool) | 400 | 400 | 未列为不支持 |
| 中途 system 消息 | 支持 | 支持 | **不支持** |
| 逐消息 effort | 支持 | 支持 | 不支持 |
| 其他 | 缓存读价为基础输入 0.025 倍（他模型 0.1） | 输出速度比 Opus 5 快 30%+ | 新分词器同文本多约 30% token；temperature 等非默认值 400 |

子代理：effort 文档把 `low` 的典型场景写为“如子代理”；选型页把 Haiku 4.5 列为“子代理任务”。注意 Fable 5.1 的 what's new 页仍写“多数负载从 Opus 5 开始”，与总览页（Opus 5.5）不一致，以总览为准。

---

## 1. effort 校准与逐消息切换

- 用途：用 effort（而非提示词）调思考量、成本、延迟。
- 做法：显式设 `output_config.effort`，各档都跑自己的 eval；不要沿用旧模型的档位名（同名档位跨模型思考量不同）。想少思考先降 effort，比提示词可靠。中途换档用 `role:"system"`、空 content、带 `output_config.effort` 的消息（beta 头 `mid-conversation-output-config-2026-07-01`），不改顶层值。
- 效果：Opus 5.5 medium ≥ Opus 5 high；Fable 5.1 medium 约等于 Fable 5 而更便宜；Sonnet 5 medium ≈ Sonnet 4.6 high。改顶层 effort 会让缓存失效，而且 Fable 5.1 上“转向效果更差”（模型倾向与之前回复一致）。
- 可能遇到的问题：Sonnet 5 low 档对中等复杂任务会想得不够；xhigh/max 思考计入 `max_tokens`，Opus 5.5 长编码回合官方建议 128k。
- xana 落地建议：`llm_driver` 层按 agent 配 effort（主 agent high/medium，子代理/分类器 low）；逐轮升降档走逐消息 effort，不改 system，不影响缓存。Kimi 无此参数，需映射到其 thinking 开关/预算并实测。

## 2. 历史只追加（思考块绑定）

- 用途：避免 400 与缓存失效。
- 做法：助手回合按 API 原样回传（含 thinking 块），请求之间不改动更早的回合。每轮提醒改用轮次作用域 system 消息；改指令、改工具用中途 system 消息/工具变更；裁剪交给服务端 compaction/context editing。客户端 compact 的最简形态是：整段历史替换为一条摘要 + 新用户消息，不回放任何 thinking 块。用 `thinking.block_binding.prefix_mismatch_behavior:"drop_block"` 跑一轮、记录 `input_transformations`，可以查出 harness 在哪里偷偷改过历史。
- 效果：2026-08-31 后新建账号强制执行；Fable 5.1 缓存读便宜 4 倍，官方建议“试试更晚再 compact”。
- 可能遇到的问题：插入后删除的每轮提醒、原地摘要、会话中途改 system，都会触发 400。
- xana 落地建议：`harness_prompt_assembly` + `memory_active_context` 定为不变式：前缀字节级稳定，加一个测试断言“相邻两次请求前缀逐字节相同”。这件事对 Kimi 同样有利于缓存，只需验证缓存命中率。与 compact 回归网相关，改动前先读规格。

## 3. 中途 system 消息（含轮次作用域）

- 用途：以运营方权重注入每轮提醒、状态变化、用户插话，同时不破坏缓存。
- 做法：在 `tool_result` 那条 user 消息之后追加 `{"role":"system", ...}`；加 `clear_at:"next_user_message"`（beta 头 `mid-conversation-system-clear-at-2026-08-21`）后只在本轮渲染，旧副本原样留在数组里、不计 token。内容写成事实而不是命令，例如“用户发来了新输入：X”“剩余 token 预算现为 Y”，不要写“忽略用户所说”。用户在工具循环中插话，也放在这里转述，让模型并入当前工作而不是另起任务。
- 效果：官方未给数据（机制性收益：缓存保持命中、thinking 块保持有效）。
- 可能遇到的问题：位置规则很严，放在 tool_use 和 tool_result 之间会 400；Sonnet 5 不支持；工具输出、检索文档这类第三方内容不能放进 system。
- xana 落地建议：harness 在消息构造层加 `TurnScopedNote` 契约，统一承载续跑提示、无进度提醒、预算变化、渠道插话；Kimi 无 system 中插，降级为 tool_result 后追加文本块（官方给出的无 beta 替代写法），需实测遵循度。

## 4. 中途增减工具（技能按需启用）

- 用途：技能激活/停用时不改 `tools`（`tools` 在缓存前缀中比 system 还靠前）。
- 做法：全部工具提前声明，未启用的标 `defer_loading:true`；之后用 system 消息中的 `tool_addition`/`tool_removal` 块启用或撤下（beta 头 `mid-conversation-tool-changes-2026-07-01`）。首轮未知的工具可用 `inline-tools-2026-09-15` 按值定义。至少保留一个非 deferred 工具。
- 效果：官方未给数据。
- 可能遇到的问题：Opus 5.5 文档明确提醒，会话中途向 `tools` 加“发消息工具”会让之前的 thinking 块失效，这类工具必须首轮就声明。
- xana 落地建议：`skills_activation` 产出“工具可见性变更事件”，由 driver 译成 tool_addition/removal；Kimi 侧维持现状（全量 tools）。

## 5. 循环内“批量独立调用”提示

- 用途：Fable 5.1 在编码/bash/computer-use 循环里，隐含的独立调用会一轮只发一个。
- 做法：每次回传工具结果后，作为轮次作用域 system 消息追加：“先私下列出你接下来需要的东西；然后在这一次回复里请求所有互不依赖的项。”
- 效果：不影响答案质量，省轮次、token 和时间（官方未给具体数字）；通用页称并行提示可把并行率推到约 100%。
- 可能遇到的问题：并行 bash 可能压垮系统性能。
- xana 落地建议：tool_dispatch 支持并发执行；提示放在第 3 节的通道里，仅编码/运维类 agent 开启；Kimi 需实测并行调用率。

## 6. 自主完成与交付范围（Fable 5.1 版）

- 用途：长任务中途不停、不问已授权的事，也不擅自缩小或扩大范围。与记忆中的 Opus 5.5 结束指令互补。
- 做法（节译，放 system，两段都用；空间不够只用第一段）：
  “你在自主运行。用户不在实时观看，任务中途无法回答问题，所以问‘要我……吗？’会卡住工作。对由原始请求推出的可逆操作，直接执行；只在破坏性操作或确需用户决定的范围变化时停下。例外：用户是在描述问题、提问或自言自语而非要求修改时，交付物是你的评估，报告后停下，不要动手修。结束回合前检查最后一段：如果是计划、分析、问题、下一步清单，或对未做工作的承诺，现在就用工具把它做完。执行改变系统状态的命令（重启、删除、改配置）前，确认证据确实支持该操作。”
  “用户的请求（或其批准的计划）就是范围，也是交付物：不要悄悄缩小、扩大或替换。某部分受阻时，把其余部分全部做完，并说明漏了什么、为什么。决定好的下一步是要执行的，不是要宣布的。”
- 效果：官方称开头“用户不在看”一句承担了大部分效果，要求原样保留（官方未给数字）。
- 可能遇到的问题：模型会更少追问含糊请求。
- xana 落地建议：放 system 的模式模块（无人值守/后台任务开启，聊天渠道关闭），会话开始时固定、中途不切换，以免破坏缓存。Kimi 需实测“描述问题时不动手”这条例外是否被遵守。

## 7. 客户端 compact 摘要的六项保留清单

- 用途：防止摘要丢掉约束、决定和精确细节。
- 做法（译）：“把对话摘要在 `<summary>` 标签中，使新上下文窗口可以直接继续，无需重做或重新提供约束。务必保留：①遇到的困难及处理方式；②提出、尝试或搁置的方案及原因；③被要求、决定、同意、排除或确立为偏好/约束/边界的内容，一字不差；④当前确切进度；⑤未决、已承诺或接下来要做的事；⑥难以重建的细节（名字、数字、日期、原话、链接），原样保留。以上六项宁长勿缺，其余从简。用户说的话要贴近原话保留；你自己的解释和推理可大幅压缩到结论。”
- 效果：官方未给数据（称 Fable 5.1“对明确告知保留什么反应良好”）。
- 可能遇到的问题：摘要变长。
- xana 落地建议：替换 compact 的摘要提示词（属 compact 回归网范围，要跑 merge_gate）；与模型无关，Kimi 可直接用，但需对比回归集。

## 8. 范围与测试克制；删掉多余的“再检查”

- 用途：Fable 5.1 会顺手修附近代码、多提交测试；Opus 5 自带自检，额外的验证指令会导致过度验证。
- 做法（译）：“工作或测试中发现既有 bug、性能问题或任务未提及的行为，除非所请求的功能离不开它，否则本次不修、不优化、不扩展，写进总结作为后续事项。任务含糊时，按措辞和周边代码最直接支持的理解实现并声明假设。临时脚本不必保留；只在任务要求或仓库已有同类测试时提交测试，规模与相邻测试相当。这里限制的只是额外内容，要求的行为要全部完整实现。”同时删除“最后加一步验证”“用子代理复核”“再检查一遍答案”这类指令。
- 效果：Fable 5.1 上未请求的改动和提交的测试代码“大幅下降，任务成功率无可测变化”；Opus 5 删除验证指令后“省 token、质量无损”。
- 可能遇到的问题：真正需要验证的长程任务，应改为“新上下文的验证子代理按间隔对照规格检查”（Fable 5 建议），而不是让模型自我复核。
- xana 落地建议：放 coding agent 的 system；审查 xana 现有技能/提示词中的“double-check”类语句。Kimi 可能仍需自检，需实测后再删。

## 9. 进度声明落地核查

- 用途：长程运行中杜绝编造的进度汇报。
- 做法（译）：“汇报进度前，把每条说法对照本会话中的某个工具结果核实。只报告你能指出证据的工作；未验证的明确说未验证。如实报告：测试失败就附输出说失败，跳过了就说跳过，做完并验证过的就直接说，不要含糊其辞。”
- 效果：Anthropic 测试中“几乎消除了编造的状态汇报”，包括专门诱导编造的任务。
- 可能遇到的问题：官方未提。
- xana 落地建议：高优先级，所有自主 agent 默认开启，放 system 的固定段；Kimi 编造倾向更需要这条，要实测。

## 10. token 预算信号：服务端 task_budget 与“别展示倒计时”

- 用途：xana 的 token 预算功能该怎样呈现给模型。
- 做法：（a）官方 `output_config.task_budget:{type:"tokens",total,remaining}`（beta 头 `task-budgets-2026-03-13`；支持 Fable/Opus 5.x，**不支持 Sonnet 5**），由服务端注入仅模型可见的倒计时，一个“agentic 回合”（到下一条不含 tool_result 的用户消息为止）共用一份预算。（b）Fable 5 指南：harness 自己显示剩余 token 倒计时，会诱发模型建议开新会话、主动交接或裁剪工作；尽量不展示，必须展示时加一句（译）：“你还有充足的上下文。不要因为上下文限制而停止、总结或建议开新会话，继续工作。”（c）Sonnet 系列有原生上下文感知，官方建议告诉它会自动 compact，“不要因预算提前停止，接近上限时先把进度存入记忆”。
- 效果：官方未给数据。
- 可能遇到的问题：客户端每轮重发全量历史，还自行扣减 `remaining`，会导致倒计时偏低、提前收尾；官方建议预算给宽，让模型自己调节。
- xana 落地建议：预算模块分两类：硬限额由 harness 强制执行；给模型的软信号只在低于阈值时，以轮次作用域 system 消息写一句事实。不要每轮显示倒计时。Kimi 需实测倒计时是否引发“焦虑式收尾”。

## 11. 长期记忆的写法规范

- 用途：让模型跨会话积累经验（Fable 5 称“表现尤其好”）。
- 做法（译）：“每条经验一个文件，顶部一行摘要。纠正和被确认的做法都记录，并写明为什么重要。仓库或聊天记录里已有的不要存；更新已有笔记而不是新建重复；发现错误的笔记要删掉。”冷启动（译）：“回顾我们之前的会话，用子代理归纳核心主题和经验，存到 [X]，并记住以后要查阅 [X]。”
- 效果：官方未给数据。
- 可能遇到的问题：官方未提；可推断存在记忆膨胀与过时的风险。
- xana 落地建议：作为 memory 工具说明和自迭代任务的提示词；冷启动可做成一次性离线任务。Kimi 需实测去重和删错的执行率。

## 12. 子代理：异步主代理、控制派生、验证者

- 用途：多代理省时间，同时防止滥派。
- 做法：（a）Fable 5.1：派生工具立即返回；结果就绪后在后续 user 消息中回传；另给一个“等待结果”工具。（b）Opus 5 派生偏多时加（译）：“只有对真正独立、可并行的大任务（如大范围多文件调查）才委派子代理。几次工具调用能完成的不要委派，不要用子代理验证你自己的工作；一个够用就不用多个。”再配合确定性上限（深度、并发、花费）。（c）Fable 5：长生命周期子代理保留上下文，靠缓存读省钱，也避免被最慢的那个拖住。
- 效果：Fable 5.1 编码任务上，主代理不等待时平均完成时间下降，质量、token、成本相近；收益来自模型选择继续干活的那些运行（它仍常选择等待）。
- 可能遇到的问题：官方明确说 prompt 中的“验证子代理”会引起过度验证（Opus 5）。
- xana 落地建议：`agents_subagents` 的 spawn 工具改为非阻塞 + `wait_subagent` 工具；硬上限放 runtime 配置；子代理默认 low effort。Kimi 需实测异步结果回注时的指代混乱。

## 13. 不要求在正文中复述推理；去掉强制 tool_choice

- 用途：规避新拒绝类别和 400 错误。
- 做法：删除“在回答中写出你的思考过程/反思”一类提示词和技能；需要推理可见性时读 thinking 块（`display:"summarized"`）。`tool_choice` 的 any/tool 改为 auto，配合 `strict:true` 或结构化输出，并在提示中说明何时用该工具。尾部预填（prefill）自 4.6 起返回 400，改为直接指令或结构化输出。
- 效果：这类提示会触发 `reasoning_extraction` 拒绝，而且服务端回退**不会**重试这一类别。
- 可能遇到的问题：旧技能里零散的“show your work”容易漏查。
- xana 落地建议：provider 适配层按模型能力表关闭强制 tool_choice；加 lint 扫描技能/提示词中的“复述推理”措辞。对 Kimi 无影响，但为蒸馏模型预留。

## 14. 代码审查：先求覆盖，另设过滤

- 用途：Sonnet 5 和 Opus 5 严格服从“只报高严重度”，召回率反而下降。
- 做法（译）：“报告你发现的每个问题，包括不确定或低严重度的。此阶段不要按重要性或置信度过滤，另有验证步骤负责。目标是覆盖。每条附置信度和估计严重度，方便下游排序。”单轮自过滤时要给具体门槛，不要用“重要”这类词。
- 效果：官方称召回下降是 harness 造成的，并非能力退化（未给数字）。
- 可能遇到的问题：噪声增多，需要第二阶段。
- xana 落地建议：审查类技能拆成“发现 → 过滤”两段；与模型无关，Kimi 可直接用。

## 15. 最终汇报再落地 + 写作密度与格式

- 用途：长时间无人值守后，用户第一眼看到的总结要读得懂；Fable 5.1 行文偏密，格式偏少。
- 做法（译）：“工具调用之间简写无妨。最终总结是给没看到过程的人：如果你已独自工作了一段时间，把它写成重新定位，而不是工作线程的延续。先写结果，再写需要用户做的一两件事，每件都当作新话题解释。不用箭头链、堆叠复合词和你自创的标签；每个文件、提交、参数都用一句平实的话交代。短与清楚冲突时选清楚。”行文过密时加一句（译）：“请去掉所有矫饰的文字。”（用比喻和修辞代替直接陈述即为矫饰。）另外要删除旧的“少用列表/加粗”规则，改成“内容多面时用列表；用户要求极简时不用；情感对话用纯文本”。
- 效果：官方未给数据。
- 可能遇到的问题：各模型倾向相反（Opus 5 偏长，Fable 5.1 偏密、偏少格式），同一段提示词不能通吃。
- xana 落地建议：放渠道/卡片层的输出风格模块，按模型选变体；位置放 user 消息更好（官方偏好），也可以避免动 system。

## 16. 低 effort 下的搜索触发

- 用途：Fable 5.1 在 low 档更爱凭记忆作答。
- 做法：优先只对该轮升档；或在 system 中加（译）：“当问题围绕一个你不能确定认识、或来自 AI 模型与开发工具这类几个月就变样的领域的名字时，名字本身就是要核实的对象：先搜索再回答，至少一个查询中按用户原样写出该名字。即使你有一些背景也照做，一知半解正是让过时答案听起来权威的原因。”
- 效果：官方未给数据。
- 可能遇到的问题：搜索次数增加。
- xana 落地建议：研究/聊天 agent 的 system 固定段；Kimi 知识截止更旧，更需要这条，需实测。

## 17. 长交付物与 max_tokens

- 用途：xhigh/max 档会先在思考中写一遍草稿再输出一遍，耗时加倍，还可能截断。
- 做法：长交付物先用 high 档；必须用高档时放宽 `max_tokens`，并在 user 消息末尾加（译）：“一次回复中包括推理和起草在内的全部产出共享约 [max_tokens] 的上限，超限用户会收到截断的回复。完整起草一遍再输出一遍只会让长度翻倍、结果不会更好，所以不要这样做。把推理用在理解需求、核对输入、定结构和难点决策上。”
- 效果：官方称可让散文和代码请求的思考“短得多”（未给数字）。
- 可能遇到的问题：官方未提。
- xana 落地建议：driver 在检测到“高档 + 长交付物”时自动追加到 user 消息，不动 system。

## 18. 杂项（小改动，收益明确）

- 定向编辑：“同等情况下尽量减少编辑文件所用的 token；不影响结果时做精确局部修改，而不是重写整个文件。”（可让 Fable 5.1 在中小改动上回到 Fable 5 水平）
- 说明原因：“我在为[谁]做[更大的任务]，他们需要[产出用途]。据此：[请求]。”（Fable 5：理解意图后表现更好）
- 安全误报：把“这段程序能否无错编译？”改问“这段程序有 bug 吗？”；冷门语言附上文档；工具不要向上下文返回 base64 数据（官方推荐直接移除）。
- 过度规划：“信息足够就行动。不要重新推导已确立的事实，不要重议用户已做的决定，不要在给用户的消息中罗列不会采纳的选项；权衡时给出建议，而不是穷举综述。”（Fable 5）
- 纠错旁白：“只有当错误会改变用户的代码、结论或决策时才更正先前说法，简短直说；无影响的小失误直接改掉，不必提。”（Opus 5）
- xana 落地建议：前两条和“过度规划”放 coding/自主 agent 的 system；base64 属于工具返回规范，写进 tools 契约文档。

---

## 汇总表

| 技巧 | 适用场景 | 优先级 | 是否需实测 |
|---|---|---|---|
| 1 effort 校准与逐消息切换 | 全部 | 高 | 是（Kimi 映射） |
| 2 历史只追加 | 全部，compact | 高 | 否（缓存命中率验证即可） |
| 3 中途/轮次作用域 system 消息 | 提醒、插话、预算 | 高 | 是（Kimi 降级方案） |
| 4 中途增减工具 | 技能激活 | 中 | 否 |
| 5 循环内批量调用提示 | 编码/运维 agent | 中 | 是 |
| 6 自主完成 + 交付范围 | 无人值守 | 高 | 是 |
| 7 compact 六项保留 | 长会话 | 高 | 是（回归网） |
| 8 范围/测试克制，删多余验证 | coding agent | 中 | 是 |
| 9 进度声明落地核查 | 长程自主 | 高 | 是 |
| 10 预算信号不展示倒计时 | token 预算 | 高 | 是 |
| 11 记忆写法规范 | 长期记忆/自迭代 | 中 | 是 |
| 12 异步子代理 + 派生节制 | 多代理 | 高 | 是 |
| 13 禁复述推理/去强制 tool_choice | provider 适配 | 中 | 否 |
| 14 审查先覆盖后过滤 | 审查技能 | 中 | 否 |
| 15 最终汇报再落地 + 文风 | 渠道输出 | 中 | 是 |
| 16 低 effort 搜索触发 | 研究/聊天 | 中 | 是 |
| 17 长交付物 max_tokens | 文档生成 | 低 | 否 |
| 18 杂项 | coding/工具规范 | 低 | 部分 |
