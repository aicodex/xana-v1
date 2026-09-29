---
title: harness prompt assembly
type: doc
status: active
---

# harness prompt assembly

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [system_prompt.py](../src/xana/harness_sdk/system_prompt.py) — system prompt 静态常量 + `build_system_prompt()`
- [compact_prompt.py](../src/xana/harness_sdk/compact_prompt.py) — compact prompt 常量 + `get_compact_prompt()`
- [default.py](../src/xana/harness_sdk/default.py) — `_session_system`（会话级 system 缓存）、`_append_note`，`_run_query_loop` 中 note 收集与每轮 tools_schema 组装
- [context_notes.py](../src/xana/harness_sdk/context_notes.py) — ContextNote 生成规则（纯函数）

## 职责

生成会话内固定的 system prompt，并把每轮变化的上下文（记忆召回、技能、按需工具、todo 提醒）以 ContextNote 追加进历史；tools_schema 每轮组装，deferred 工具被揭示后下一轮即生效。

## system prompt 结构

### 静态段（system_prompt.py 模块级常量）

通用核心，不带写代码的规则（在 `coding` 技能，`skills/bundled/coding.py`，写代码前经 `skill_load` 加载），也不写 xana 没有的机制（钩子、交互式权限审批、`/help`）。设计见 [prompt-structure-design](../docs/design/prompt-structure-design.md)。

1. `INTRO` — 中性身份（"通过聊天渠道和命令行为用户工作的助手"）与不编造 URL
2. `SYSTEM` — 输出即沟通（Markdown 按渠道渲染）、被拒调用不原样重试、prompt injection 警觉、自动压缩
3. `DOING_TASKS` — 执行任务总则（探索性问题给推荐、失败先诊断、不超出要求）
4. `ACTIONS` — 谨慎执行操作（风险操作清单）；末尾"如实报告结果"一句译自 CC 2.1.281
5. `USING_TOOLS` — 专用工具优先、TodoWrite、并行调用
6. `TONE` — 语气与风格
7. `COMMUNICATING` — 与用户沟通（译自 CC 2.1.281「# Communicating with the user」：写给追进度的队友、第一次调用工具前说一句、最终消息自足）
8. `OUTPUT` — 输出效率
9. `ANSWER_FORMAT` — 回答格式（改写自 Codex「Final answer structure」：按任务大小定格式、列表不嵌套、结论先行、给出处）

### system 拼装（`build_system_prompt()`，会话内固定）

默认提示词段序：INTRO、SYSTEM、DOING_TASKS、ACTIONS、env、tools、extension_sections、USING_TOOLS、TONE、COMMUNICATING、OUTPUT、ANSWER_FORMAT，最后是 profile_tail。规则：

- **profile_tail**（`profile_sections(PromptProfile)`）：会话级开关文本，固定在最末，顺序为进度核查 → 广泛查看 → 时间 → 已答视为完成 → 自主运行段（`mode=unattended` 时）→ 长期指令。交互默认为空（照 CC：交互不额外干预）；原则是少干预、重验证，完成判定交给 verify 与目标评估器。

- **会话内固定**：`DefaultHarness._session_system` 按 (session, agent) 缓存首轮生成的 system，之后每次请求原样复用。会话内换模型不改 system，改为追加 `model_switch` note（Codex 同款）。依据与缓存实测见 [turn-control-design](../docs/design/turn-control-design.md) §1–§2。
- **agent 模板**（`config_snapshot["agent_system_prompt"]`，即 AgentSpec.system_prompt）非空时它就是 system：`${base_prompt}` 替换为上面的完整默认提示词（不含 profile_tail），`${env}` 替换为环境段，`${extensions}` 替换为扩展片段，未知占位符原样保留；不含占位符 = 完全替换，除 profile_tail 外不追加任何内容（设计见 [agent-composition-design](../docs/design/agent-composition-design.md) §3.4）。
- env 段（`env_section()`）：cwd、是否 git 仓库、平台、shell、OS、模型名（取会话建立时的模型）。
- tools 段（`tools_section()`）恒为空字符串——完整 schema 走 `ModelRequest.tools_schema`（CC 同款）。
- extension_sections：extension `PromptAssembler`（`ext.context`）的输出。契约要求 **ContextContributor 输出会话内稳定**；因 system 按会话缓存，首轮输出即本会话版本。

### ContextNote：每轮变化的上下文（`harness_sdk/context_notes.py`）

会话内会变的上下文不再进 system，而是以 `ContextNote`（`context/message.py`）追加进历史：模型看到的是一条 user 角色的 `<system-reminder>` 文本。规则（照 Claude Code / Codex / Kimi Code）：

- **写入历史并持久化**：随 active context 与 history jsonl 保存；fork run 产出的 note 随 PH 替换写回主历史。
- **只追加、不改写**：旧 note 原样保留，保证相邻两次请求前缀逐字节一致（`tests/test_prefix_stability.py`）。
- **只在变化时注入**：注入前扫描历史里同类最后一条 note，内容相同即跳过。
- **位置**：紧跟触发它的消息（用户消息之后或工具结果之后）。

| kind | 何时追加 |
|---|---|
| `date` | 与环境段同条件（`_has_env`）：会话开始给一次"今天是 YYYY-MM-DD"，跨天追加"日期已变更"（CC 同款，只给日期） |
| `project_instructions` | 与环境段同条件：项目根目录（`context.cwd`）有 `AGENTS.md` 时，每条用户消息前读一次，内容变化才注入；空文件不注入，超过 2 万字截断并提示用 Read 看全文；措辞译自 CC 的 CLAUDE.md 注入 |
| `memory_recall` | agent 可用 memory 组工具时，每条新用户消息之后，有未注入过的命中时。截断提示"用 Read 查看"仅在可用 Read 时出现。按文件路径去重，跳过模型已 Read 的文件；单次 ≤5 个文件、每个 ≤4KB / 200 行；会话累计 60KB 后停止；检索词少于 2 个不召回（中文不以空格判断） |
| `turn_input` | 技能 prompt blocks + extension `contribute_turn_input` 片段，内容变化时。贡献者经 `TurnInputContext.available_tools` 得知本 agent 的可用工具（技能列表仅在可用 `skill_load` 时列出），经 `TurnInputContext.visible_skills`（`_visible_skills(context)` 取 `config_snapshot["skills"]`，None = 全部）得知可见技能（技能列表只列这些） |
| `deferred_tools` | 未揭示的 defer 工具名单（名字: 描述）变化时；名单清空且曾列出过时追加"均已加载"。agent 不可用 `tool_search` 时不隐藏任何工具（全部给完整 schema），也就没有这条 |
| `todo_reminder` | agent 可用 TodoWrite 时，距上次 TodoWrite 与距上次提醒都满 10 个 assistant 轮次，附当前清单（CC `TODO_REMINDER_CONFIG`） |
| `model_switch` | 会话内切换模型后 |

compact 摘要覆盖其前的 note；去重集合与累计量都靠扫描历史得出，compact 后自然重置并重新注入。

### 事实来源

静态段最初逐字译自 CC 2.1.88（`constants/prompts.ts` 底版）；2026-09 对照 CC 2.1.276 做过一次克制微调（删除 CC 已删条目、补 CC 新增条目）；2026-09-29 拆出编码规则到 `coding` 技能、删去 xana 没有的机制、加入 Codex 回答格式。env 段模板逐字来自 AGENT_LOOP_PROMPT.md:123-130。note 文案（todo 提醒、按需工具）译自 CC 2.1.281。

## compact prompt（compact_prompt.py）

`get_compact_prompt()` = `NO_TOOLS_PREAMBLE` + `BASE_COMPACT_PROMPT` + `NO_TOOLS_TRAILER` 三段拼接：

- 头/尾两次强调"仅回复纯文本、禁止工具调用"（CC 同款：工具调用浪费唯一回合）。
- 正文要求先 `<analysis>` 后 `<summary>`，摘要固定 9 个小节（见 [harness_compact](harness_compact.md)）；含安全约束逐字保留与"仅 user 角色回合算用户消息"两段（2026-09 对照 CC 2.1.276 补入）。
- `COMPACT_BOUNDARY_MARKER = "对话已压缩"` 也定义在本模块。
- 本模块只管"让模型写摘要"的 prompt；"把摘要包给后续 prompt 看"的模板（`CompactSummary.POST_COMPACT_TEMPLATE`）单一来源在 `context/message.py`（§1.9 single-source rule）。

## 不变量

- system 会话内固定；每轮变化的上下文只以 ContextNote 追加，不得回到 system（前缀稳定测试守护）。
- 项目级规则文件（CLAUDE.md 类）目前不进 system prompt；CC 是包 `<system-reminder>` 作为 messages[0] prepend，xana 预留了同样位置（default.py 中 TODO(claude_md)）。
