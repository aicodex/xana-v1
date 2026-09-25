---
title: 回合控制与提示词组装(P2.5)
type: design
status: draft
date: 2026-09-24
---

# 回合控制与提示词组装(P2.5)

> 状态:**草案 v3**。v2 按 Claude Code 2.1.88 / Codex / Kimi Code 源码实证修订;v3 按最新 Claude Code 2.1.281(2026-09-23,二进制抽取,见 `~/project/best_agent/claude-code-2.1.281-prompts/HARNESS_MECHANISMS.md`)校正 §2、§5、§6、§7。
> 本文不改变现行契约;确认后按 §10 分阶段落地,每阶段同步更新 doc_ai 与测试。提示词均为中文版。

## 0. 目标与非目标

**目标**

1. **system 与 tools 会话内固定**;每次都会变的内容改为在产生处追加进历史,只追加、只在变化时注入。变化只落在"最后一个请求"的末尾。
2. **续跑有来源、有上限**:沿用三家共有的续跑点(stop 决策 + 后台任务完成唤起),无人值守另设目标模式。
3. **可见进度**、**预算信号**、**按 agent 的提示词开关**(Anthropic 官方指南)。

**非目标**:子 agent Task 工具与结果回传(P2);预算硬上限接通(P3)。(v2 曾以"三家都没有"排除完成检查器;CC 2.1.281 的 /goal 已用独立评估器,v3 纳入 §6.3。)

## 1. 问题:xana 的 system 为什么每轮在变

每次请求由 `_assemble_for_model(system_content, history)`(`harness_sdk/default.py:1284`)拼成:

```
[ system(每次现拼) , 历史1 , 历史2 , … , 历史N ]      ← history 已固化,system 不属于历史
```

`system_content` 由 `build_system_prompt` 在回合循环内现拼,其中四处会变:

| 来源 | 变化时机 | 位置 |
|---|---|---|
| 记忆召回 `SearchMemoryInjector.select(user_input)` | 每条新用户消息(检索词不同) | `default.py` ~684,拼入 system 尾部 |
| `<todo_status>` | 每次 run 开始时重读 TodoStore | `default.py` ~714 |
| 按需工具名单 | `tool_search` 揭示工具后(同时 tools 数组也变) | `default.py` ~964 |
| 技能列表(turn_input 片段) | 技能增删时 | `extension_api/builtin/skills_listing.py` |

**代价(Kimi 实测,2026-09-24)**:前缀缓存按最长公共前缀、以 256 token 为块匹配。

| 步骤 | 输入 | 命中 |
|---|---|---|
| 首次 / 完全相同 | 5914 | 0 / 5914 |
| 改历史第 3 条中间 | 5916 | 4864(改动点之前全命中) |
| 固定改后版本再发 | 5916 | 5916 |
| 改 system 开头 | 5917 | **0** |

system 只有一个位置且在最前:第 5 轮换了问题 → 记忆召回变 → system 变 → 其后 4 轮历史全部未命中。对话越长损失越大。

## 2. 依据:三家源码实证

| | Claude Code 2.1.88 | Codex(codex-rs) | Kimi Code |
|---|---|---|---|
| system | 各段缓存到 /clear、compact(`systemPromptSections.ts`) | `base_instructions` 会话开始生成并存 rollout;换模型追加 developer 消息而不改 instructions | 绑定时渲染一次存事件日志;**日期刻意不进 system**(`system.md:61`) |
| 动态上下文 | attachment 追加进历史(内存),normalize 时渲染为 `<system-reminder>` | WorldState 分段,只追加变化的 diff,持久化并在 resume 重放 | reminderService 以 user `<system-reminder>` 追加并持久化;翻历史找自己上次注入去重 |
| 每次附加但不记录 | 仅 CLAUDE.md + 日期放 messages[0],memoize 到 compact(内容不变) | 无 | 无 |
| todo | 约 10 轮未 TodoWrite 才提醒一次 | 计划只是普通工具调用记录 | 同 CC,N 轮未更新才提醒 |
| compact | 历史换成摘要 + 重新生成的当前状态 | 摘要后下一轮全量重注入 | 丢弃全部注入消息,摘要后重新注入 |
| 续跑来源 | stop hook、token 预算提醒、输出超长恢复、后台任务完成通知 | stop hook、待处理输入、代理间消息 | stop hook(每回合限一次)、后台任务完成、goal 模式 |
| todo 未完成续跑 | 无 | 无 | 无 |
| 轮数上限 | 有 | 无 | 可选(环境变量) |

**Claude Code 2.1.281 的新增与变化**(优先于 2.1.88):

| 项 | 2.1.281 做法 |
|---|---|
| 每轮提醒载体 | 新增会话中途 `role:"system"` 消息(beta `mid-conversation-system-2026-04-07`):每轮的提醒合成**一条**,放在 tool_result 之后(todo、无进度、token 倒计时、日期变化、plan 模式、工具变更);上下文类(CLAUDE.md、部分记忆)仍在 user 消息;模型/服务端不支持时退回 user `<system-reminder>` |
| 无进度提醒 | 连续 5 次**模型调用**没有对用户输出即注入一次,同一静默期最多 3 次;输出非空文本或调用对用户可见的工具即清零;只在主线程工具循环中间轮;留在历史 |
| 续跑组合 | 只有 thinking 无文本推 1 次;输出截断续写 ≤3 次;工具调用格式错重试 1 次;Stop hook / 评估器连续阻断上限 8 次;**没有 todo 续跑** |
| /goal | 会话级 prompt 型 Stop hook:每轮结束由**独立评估器**判断目标条件是否满足,不满足就阻断结束;后台任务运行时暂缓评估,每 30 分钟 check-in(指数退避,空闲上限 3 次) |
| 自主运行段 | 仅非交互 / 自主场景加入 system:"结束本轮前检查最后一段;如果是计划、分析、问题、下一步清单或还没兑现的承诺,现在就用工具去做。" |
| 记忆召回 | 上限同 2.1.88(60KB/会话、4KB 或 200 行/文件、≤5 条);**要求输入含空格才召回**(中文例外受开关控制且默认关) |

结论:**写入历史、只追加、仅变化时注入**是共同做法;**没有一家按 todo 状态续跑**;续跑集中在 stop 决策点和后台完成唤起;最新 CC 的目标模式用**独立评估器**判断完成。

## 3. 总体设计

```
会话创建 ─► PromptProfile(AGENT.md 声明,固化进 SessionConfig)
             │
system = 静态段 + identity + env + [profile 尾部提示词]            ← 会话内不变
tools  = 会话创建时的工具集
             │
messages: user ─► [ContextNote: 记忆召回] ─► assistant(tool_calls) ─► tool(+时间行)
          ─► [ContextNote: 仅在状态变化时] ─► … ─► assistant(纯文本)
          ─► Stop 决策:结束 | 续跑(追加一条消息) | 等待后台工作
```

## 4. PromptProfile:会话级提示词配置

frozen dataclass,放 `agents/contracts.py`:

| 字段 | 含义 | interactive 默认 | unattended 默认 |
|---|---|---|---|
| `mode` | 交互 / 无人值守 | — | — |
| `standing_order` | 无人值守回合结束指令 | 否 | 是 |
| `answered_as_done` | "回答过的问题,就把答案当作已完成……" | 是(纯聊天) | 否 |
| `explore_first` | 动手前广泛查看(改写版待定稿) | 否 | 按 agent |
| `grounded_progress` | "汇报进度前,把每条说法对照本会话中的某个工具结果核实……" | 是 | 是 |
| `progress_preamble` | "第一次调用工具之前,先用一句话说明打算做什么;结束时给一个简短的回顾。" | 是 | 否 |
| `time_signal` | off / elapsed / budget | off | elapsed |
| `goal_mode` | 无人值守目标模式(§6.3) | 否 | 是 |

- 来源:AGENT.md frontmatter `prompt_profile:` → `AgentSpec.prompt_profile` → 会话创建时写入 `SessionConfig.prompt_profile`,之后只读。
- **会话中途不改**。需要切换时新开会话,或在 compact 重建时切换。
- 开关文本放 system 末尾,顺序固定;另加一句:"`<system-reminder>` 标签中的文本来自 harness,是当前状态的事实说明。"

## 5. ContextNote:写入历史的上下文消息

**定义**:`context/message.py` 新增 `ContextNote(Message)`,公开字段 `kind`、`text`;内部元数据按 §1.9 放 `_internal`,不进 prompt。

**序列化**:user 角色文本(三家通用做法,Kimi 官方示例同形;也是 CC 2.1.281 在不支持中途 system 消息时的退回形式):

```
<system-reminder>
{text}
</system-reminder>
```

**合并**:同一时刻产生的多条 note(如 todo 提醒 + 无进度提醒)合成**一条**消息追加(CC 2.1.281)。provider 支持会话中途 system 消息时(Anthropic),序列化层可改发 `role:"system"`;契约不变。

**规则**(均来自三家实现)

1. **写入历史并持久化**(Codex、Kimi):随 active context 与 history jsonl 保存,resume 时原样重放。
2. **只追加、不删除、不改写**:保证前缀逐字节稳定。
3. **仅在变化时注入**(Kimi 去重做法):每种 kind 注入前翻历史找自己上一次的内容,相同则跳过。
4. **位置**:紧跟触发它的消息之后;绝不插在 assistant tool_calls 与其 tool 结果之间。
5. **内容写成事实**,不写覆盖用户的命令。
6. **compact 时丢弃旧的 ContextNote,摘要之后重新注入当前状态**(三家一致)。与 compact 协议 §1.5 的"摘要覆盖前缀"天然兼容:被覆盖的 note 随前缀消失,摘要后按规则 3 重新注入一次。

**kind 清单**

| kind | 注入时机 | 依据 |
|---|---|---|
| `memory_recall` | 每条新用户消息之后(有新的、未注入过的命中) | CC `relevant_memories`,细则见下 |
| `skills_listing` | 会话首条消息;技能集合变化时 | CC skill_listing、Kimi skills reminder |
| `deferred_tools` | 会话首条消息;按需工具名单变化时 | CC deferred_tools 增量 attachment |
| `todo_reminder` | 连续 10 轮 assistant 未调用 TodoWrite 且距上次提醒 ≥10 轮 | CC `TODO_REMINDER_CONFIG`、Kimi todoListReminder |
| `model_switch` | 会话内切换模型后的下一次请求 | Codex `<model_switch>` developer 消息 |
| `background_result` | 后台任务 / 子 agent 结束(P2) | CC、Kimi 任务完成通知 |
| `no_progress` / `token_budget` / `continuation` | 见 §7、§8、§6 | Anthropic 指南 |

**记忆召回细则**(照搬 Claude Code `relevant_memories`,`utils/attachments.ts`):

- 每条新用户消息检索一次,检索词取最后一条真实用户消息;过短的输入不检索。**注意**:CC 以"输入含空格"判断,中文没有空格,不能照抄;xana 用 `search_terms()` 切出的检索词数判断(<2 个不检索)。
- 结果追加为一条 `memory_recall` note,紧跟本条用户消息;role 为 user(`<system-reminder>` 包裹),不是 system。
- **按文件路径去重**:注入前扫描历史里已注入的记忆路径,已出现过的、以及模型已用 Read / memory_search 读过的文件跳过;同一份记忆在一个会话内只出现一次。
- **单次上限**:最多 5 个文件,每个截到 4KB 或 200 行,截断时附"完整内容请用 Read 查看 {路径}"。
- **会话累计上限**:历史中召回内容累计达 60KB 即停止召回(CC 线上观测长会话约 26K token,约三次完整注入后最相关的记忆已在上下文中)。
- **字节稳定**:每条记忆的标题头在注入时生成并存入 note,之后渲染用存下的版本。
- 去重集合与累计量都通过扫描历史得出,compact 后旧 note 消失,两者自然清零,重新开始召回。

**迁移**:`build_system_prompt` 删除 `dynamic_middle`;记忆、技能、按需工具名单、todo 全部改为上表的 note(原位替换,不保留双路径)。`<todo_status>` 每轮块删除,改为 `todo_reminder`。`ContextContributor` 契约补"输出会话内稳定";`TurnInputContributor` 的片段改为 note。env 段的模型名:切换模型时 system 不变,追加 `model_switch` note。

**待实测**:`tool_search` 揭示工具会改变 tools 数组,Kimi 端 tools 变化是否使整条缓存失效未知。阶段 A 用 §1 的探针脚本测;若失效,改走 Kimi 的"会话中途 system 消息携带 tools"(Kimi Code `toolSelectSchemasService.ts`,需确认 kimi-for-coding 支持)或会话创建时即给全量 schema。

## 6. Stop 决策与续跑

`_run_query_loop` 中 `if not iter_tool_calls:` 处改为 Stop 决策(同模块普通函数,不新增 ABC),按顺序:

1. abort / 预算硬超限 / episode 超限 → **结束**。
2. **stop 续跑点**(§6.1)返回续跑理由 → **续跑**。
3. 本 run 启动的后台工作未结束(P2)→ **等待**,结束后以 `background_result` 追加并再调用模型。
4. `goal_mode` 且目标未报告终态(§6.3)→ **续跑**。
5. 其他 → **结束**。

**上限**(照 CC 2.1.281 的组合):只有 thinking 无文本推 1 次;输出截断续写 ≤3 次;工具调用格式错重试 1 次(xana 已有 `input_error` 回模型);stop 续跑点 / 评估器**连续**阻断上限 8 次;工具轮上限改读 `AgentSpec.max_turns`(缺省 25,CC 同样有上限)。达上限结束并在 final 事件标注 `continuation_exhausted`,等人查看。每次续跑发 `HarnessEvent(kind="continuation")`。

### 6.1 stop 续跑点(三家共有)

- 扩展 `TurnLifecycleContributor.on_turn_stop`:返回值由 `None` 改为 `str | None`,非空即续跑理由(对应 CC/Codex/Kimi 的 Stop hook)。
- 理由以 `stop_hook` note 追加("Stop hook 反馈:{理由}"),然后再调用模型。用 note 而非普通 user 消息,是为了标明它不是用户输入(对应 CC 的 isMeta);只有思考 / 截断续写用 `continuation` note。hook 的输入带 `final_text` 与 `continuation_active`(对应 CC stop_hook_active)。(阶段 B 实施时修订)
- **连续阻断上限 8 次**(CC 2.1.281);Kimi 为每回合 1 次。取 CC 值,达上限结束并标注 `continuation_exhausted`。
- 这是对 Extension API 契约的修改,实施前需确认(§11)。

### 6.2 todo 续跑(Anthropic 指南,可选策略)

- 三家都没有内置;Anthropic Opus 5.5 指南推荐。实现为一个内置 stop contributor,**默认关闭**,按 agent 开启并做 A/B。
- 触发:todo 存在 pending / in_progress 项。续跑理由用官方模板:
  > 你的任务清单里还有未完成的项:{剩余项}。请继续完成。如果其中某项受阻,请说明是什么阻碍了它。
- 开启此策略时,TodoStatus 需加 `blocked`、`cancelled` 终态(OpenAI 计划卫生建议),否则合法受阻的任务会被反复拉起。未开启时不改 TodoStatus。

### 6.3 目标模式(CC 2.1.281 /goal + Kimi Code goal)

- 实现在 harness Stop 决策内(`DefaultHarness._goal_decision` + `harness_sdk/goal.py`),位于 stop hook 之后,与 stop hook 共用连续拦截上限 8。不做成 extension contributor:评估器需要模型客户端与会话 profile,做成 contributor 只能靠隐式注入(违反 AGENTS.md 规则 7)。(阶段 C 实施时修订)
- 目标条件在会话开始时给定(无人值守 run 默认用任务文本)。注入目标说明(CC 译文,按 xana 改写):
  > 目标模式已开启,条件为"{条件}"。简短确认目标,然后立刻开始(或继续)朝它推进。把条件本身当作指令,不要停下来问该做什么。条件成立之前,每轮结束都会被拦下继续。
- 每轮正常结束时由**独立评估器**(小模型一次调用,只看目标条件与最近输出)判断是否满足;不满足则以评估器给出的理由续跑,满足则结束。
- 预算耗尽、被取消、出错视为终态(Kimi `blockIfBudgetReached`)。
- 后台工作运行中暂缓评估;P2 落地后按 CC 的 check-in 节奏(默认 30 分钟,指数退避,空闲上限 3 次)。
- 模型也可调用 `report_goal`(done / blocked / failed + 原因)主动声明终态,blocked / failed 直接结束,不再进入评估。

**无人值守 system 段**:`mode=unattended` 时,system 末尾除 standing_order 外,加入 CC 的自主运行段:
> 结束本轮前检查最后一段;如果是计划、分析、问题、下一步清单或还没兑现的承诺,现在就用工具去做。

### 6.4 命令型验证(CC 命令型 Stop hook,2026-09-25 增补)

- 原则(用户):模型越新越少干预,给探索空间;必要的是验证方式——它决定能否完成、做得好不好。
- AGENT.md `verify:` 声明检查命令(字符串或 `{cmd, timeout_s}`,默认 300 秒),由 agent 作者给出,模型不能增改。
- Stop 决策顺序:stop hook → **verify** → 目标评估器。模型以纯文本结束一轮时在工作目录依次执行,遇第一个未通过即停;任一非 0 / 超时 → 以 `verify` note 反馈命令、退出码与输出末尾("Stop 验证未通过,命令 …"),续跑;与 stop hook、评估器共用连续拦截上限 8。CC 用退出码 2 表示有意拦截,这里是验证语义,非 0 一律未通过。
- 全部通过 → 通过结果作为证据交给目标评估器(不写入历史);验证未通过时不调用评估器。final 事件带 `verify_status`(passed / failed)。
- 命令在独立进程组运行;超时或用户取消时整组结束,不留孤儿进程。

## 7. 进度更新

- **归一**:与 tool_calls 同一轮出现的 assistant 文字判定为进度说明,发 `HarnessEvent(kind="progress_update")`;无 tool_calls 那轮的文字是回答。Anthropic 将来的 progress-update thinking 块映射到同一事件。
- **无进度提醒**(参数照 CC 2.1.281):连续 5 次**模型调用**没有对用户输出(无非空文本、无对用户可见的工具)时,在最新工具结果后追加 `no_progress` note;只在工具循环的中间轮,不在用户刚发消息那轮,子 agent 不注入:
  > 用户已经有一段时间没收到你的消息了——用几句话说明你在做什么,然后继续。

  保留原位,同一静默期最多 3 次;有输出即清零。
- **卡片显示**(阶段 D 已落地,用户确认方案):卡片进度行见 session-and-card-state §5.5a;`final.text` 只含回答轮文字,进度说明在 `progress` 列表;完成后进度行收起。

## 8. 预算信号

- **时间**(Anthropic Opus 5.5 指南;CC 2.1.281 本身未实现"已用 N 秒 / M 秒",当前时间走结构化字段):`budget` 时在每条工具结果末尾追加"已用 340 秒 / 1200 秒";`elapsed` 时只追加"已用 340 秒",并在 system 尾部加"时间在这里很重要:不要花费可以避免的时间,越早得到正确的结果越好。"建议性;硬截止靠 run timeout(P3)。
- **token**:不常驻倒计时(Claude 官方警告会诱发提前收尾)。跨 50% / 80% / 95% 时各追加一次 `token_budget` note,只写事实。需 A/B,依赖 P3。

## 9. 契约改动清单

| 位置 | 改动 | 兼容性 |
|---|---|---|
| `agents/contracts.py` | + `PromptProfile`;`AgentSpec` + `prompt_profile`、`time_budget_s` | 新增字段有默认值 |
| `session_config/contracts.py` | `SessionConfig` + `prompt_profile` | 旧会话按 interactive 默认 |
| `context/message.py` | + `ContextNote`;ContextSDK + `append_note`、`last_note(kind)` | 新增 |
| `harness_sdk/system_prompt.py` | 删除 `dynamic_middle`;+ profile 尾段 | **行为变化**:所有会话 prompt 结构改变 |
| `harness_sdk/contracts.py` | HarnessEvent kind + `progress_update`、`continuation` | 各 adapter 需忽略未知 kind(核对) |
| `extension_api/interfaces.py` | `on_turn_stop` 返回 `str \| None`;ContextContributor 文档补"会话内稳定" | **接口修改,需确认** |
| `tools/builtin/` | + `report_goal`(goal_mode);TodoStatus 终态仅在开启 §6.2 时加 | 按需 |
| `docs/design/session-and-card-state.md` | §1.9 增 ContextNote 序列化;§5 进度显示(阶段 D) | 改锁定契约,**需确认** |

**破坏性影响**:system 结构变化使线上已有会话升级后第一轮缓存全失一次;旧历史照常可用(无 note 的历史只是少了召回块)。compact 按现有规则覆盖旧 note,摘要后按 §5 规则 6 重注入。

## 10. 分阶段实施

| 阶段 | 内容 | 验收 |
|---|---|---|
| **A 稳定前缀** | ContextNote + `append_note`/去重;记忆、技能、按需工具名单、todo 迁出 system;compact 后重注入;tools 变化缓存实测 | 测试断言"同会话相邻两次请求,新请求以上一请求的全部内容为前缀";merge_gate 39+1;探针脚本多轮缓存命中 ≥ 上一轮输入 |
| **B Stop 决策** | 决策点;`on_turn_stop` 续跑(每回合一次);max_turns 读 AgentSpec;continuation 事件 | 单测覆盖各分支与上限 |
| **C 提示词开关 + 目标模式** | PromptProfile;system 尾段;`report_goal` 与 goal 续跑 | 单测;无人值守 live 脚本多步任务跑完并报告终态 |
| **D 进度** | progress_update;无进度提醒;卡片显示(先改规格) | 单测 + driver_scenarios |
| **E 预算信号** | 时间预算"已用 N 秒 / M 秒";token 阈值提醒(随 P3)。("已用 N 秒"已随阶段 C 落地,因 unattended 默认 time_signal=elapsed) | 单测;token 信号 A/B |
| (可选)todo 续跑 | §6.2 内置 stop contributor + TodoStatus 终态 | A/B 后决定默认值 |
| (P2) | 后台工作等待 + background_result | 随 Task 工具验收 |

## 11. 待确认问题

1. **`on_turn_stop` 返回续跑理由**:修改 Extension API 接口(§6.1),是否同意?
2. **目标模式**:v3 改为 CC 2.1.281 的"独立评估器 Stop hook",`report_goal` 保留为模型主动声明终态的补充(§6.3)。与 v2 已确认的"只用 report_goal"不同,需重新确认。
3. **todo 续跑**:作为可选策略、默认关闭(§6.2),是否同意?
4. **卡片显示进度说明**:阶段 D 改卡片规格 §5,是否纳入本期?
5. **explore_first 改写文本**:信息源名词草拟为"日志、运行记录、记忆树中的规则、项目文件",是否定稿?
