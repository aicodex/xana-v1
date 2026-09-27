---
title: agent 切换、委派与创建
status: active
date: 2026-09-27
---

# agent 切换、委派与创建

## 1. 目标

- 用户能让一个会话由指定 agent 负责(`/agent <名字>`);
- 模型能把子任务委派给其他 agent(`agent_run` 工具),同步取回结果,可并行、可续接;
- 模型能帮用户创建 agent(`agent_define` 工具 + `agent_creator` 技能),写出 `~/.xana/agents/<name>/AGENT.md` 并立即可用。

不在本次范围:后台委派(run_in_background)、fork 父上下文、子 agent 指定模型、容器隔离。

依赖:[agent-composition-design](agent-composition-design.md)(工具组、tools 表达式、system_prompt 模板)。

## 2. 参考(Claude Code / Codex / Kimi Code 源码调研)

- **三家共识**:子 agent 默认空上下文;只交回最终文本,主 agent 负责转述(用户看不到子 agent 输出);子 agent 工具只能从父范围收窄;都有递归防护;**会话在创建时绑定主 agent,之后不切换**(切换会同时换 system 与工具,缓存必然失效,历史里还可能有新 agent 没有的工具调用)。
- **递归防护**:CC 2.1.88 从子 agent 工具集里直接去掉 Agent 工具;CC 2.1.281 深度上限 3;Codex 默认深度 1;Kimi 内置子 agent 没有 Agent 工具。
- **agent 列表**:CC 2.1.281 从工具描述挪到消息附件(源码注释:写在工具描述里时占全部缓存写入约 10%);Kimi / Codex 拼在工具描述里。
- **创建**:CC 2.1.281 删掉了 `/agents` 向导,改为"让模型直接写 `.claude/agents/*.md`";Kimi / Codex 无创建器。
- **委派提示词**(CC / Kimi 原句转述):不要把"理解问题"也委派出去,先自己弄清楚再写任务说明;子 agent 结果用户看不到,要转述;互不依赖的子任务在同一条消息里并行发出。

## 3. 设计

### 3.1 会话绑定 agent:`/agent`

- `SessionConfig.agent: str | None` 记录会话绑定的 agent;`None` = 默认 agent(chat)。
- `DefaultAgentRequestRouter` 读会话配置决定 `target_agent`(构造注入 `SessionConfigStore`)。绑定的 agent 已不存在时,gateway 照常报 `unknown agent`,不静默回退。
- `/agent`:无参数列出可用 agent(名字、说明,按钮点选);`/agent <名字>` = **换一个新会话 id 并绑定该 agent**(与 `/clear` 同样的换 id 机制),旧会话原样保留。
- `/clear` 换 id 时把原会话的 agent 绑定带到新会话(清空上下文,不换 agent)。

### 3.2 委派工具:`agent_run`

输入:

| 字段 | 说明 |
|---|---|
| `description` | 3–8 个字的任务标题(显示用) |
| `prompt` | 给子 agent 的完整任务说明 |
| `subagent_type` | agent 名;默认 `chat` |
| `resume` | 续接之前某个子 agent 的 agent_id(与 `subagent_type` 互斥):在同一子会话里继续,保留它的上下文 |

- 同步执行,经 `AgentRunner` 跑子 agent 的完整 run,取回最终文本;执行中以 `report_progress` 上报子 agent 的工具调用次数。`concurrency="parallel"`,同一轮多个调用并发执行;进程内同时运行的委派上限 6,超出返回错误:"已达并发上限,不要重试,直接用自己的工具完成"。
- 结果:`agent_id`(子会话 id)、状态、用时、工具次数,加子 agent 的最终文本;末尾提示"需要继续这个子 agent 时用 resume=<agent_id>"。子 agent 失败时 `ok=False`,附原因。
- **子 agent 的工具** = 子 agent 自己的 tools 表达式解析结果 ∩ 上限集合。上限集合 = 调用方本 run 可用的工具 − `agent_run` / `agent_define` − 会话控制类工具(EnterPlanMode、EnterWorktree、report_goal、Cron*)。
  - 上限通过 `AgentRequest.tool_ceiling`(新增类型化字段)传给子 agent,harness 在 `resolve_tools` 结果上再取交集;调用方可用工具经 `ToolContext.available_tools`(新增字段)拿到。
  - 子 agent 永远拿不到 `agent_run`,委派深度固定为 1,不需要深度计数(CC 2.1.88 做法)。
- 子会话 id:`{父会话}-agent-{短 id}`;`SessionRecord` 记录 `parent_session_id` / `parent_run_id` / `depth=1`。
- 取消:父 run 被取消时子 run 一并中止(沿用 GatewayAgentRunner 的 AbortSignal 联动)。
- 工具描述(中文)要点:何时委派 / 何时自己做;写任务说明要像给刚进门的同事交代(目标、已知信息、交付物、边界);不要把理解问题委派出去;结果用户看不到,要转述;并行的多个委派放在同一条消息里;不要编造未返回的结果。

### 3.3 可委派 agent 列表:随工具注入

- 新增 `AgentListingContributor`(`TurnInputContributor`,`extension_api/builtin/`):本 agent 可用 `agent_run` 时,每条用户消息把可委派 agent 列表作为 note 追加(内容不变不重复注入,沿用 `changed_note`),不写进工具描述,保持工具 schema 稳定。
- 每行:`- 名字: description｜when_to_use(工具: 展开后的有效工具)`;按名字排序,内容只随 agent 定义变化。
- AGENT.md 新增可选字段 `when_to_use`(映射 `AgentSpec.when_to_use`)。

### 3.4 创建 agent:`agent_define` + `agent_creator` 技能

`agent_define` 工具,两个动作:

- `catalog`:返回工具组目录(组名、说明、是否默认、包含的工具)、未分组工具、已有 agent 名单——相当于 DSH 的 inspect。
- `create`:字段 `name`、`description`、`when_to_use`、`tools`、`disallowed_tools`、`system_prompt`、`overwrite`。校验:
  - `name`:小写字母开头,`[a-z0-9-]`,2–40 字符;不得与内置 agent 重名;已存在时须 `overwrite=true`;
  - `tools` / `disallowed_tools` 里的名字必须是已登记的组名 / 工具名 / `default` / `*`;
  - 新 agent 解析出的工具 ⊆ 调用方本 run 可用的工具(不能借创建提权);
  - `system_prompt` 至少 20 字。
  - 通过后写 `~/.xana/agents/<name>/AGENT.md`,经 `FileAgentRegistry` 同一套解析重新加载并注册进 gateway,立即可用(`/agent <name>` 或 `agent_run`)。
- `~/.xana/agents` 仍对 Write / Edit 保护;`agent_define` 是唯一写入口。
- xana 目前没有交互式审批("ask" 按拒绝处理),**用户确认在对话中完成**:`agent_creator` 技能要求模型先用 `catalog` 了解可选工具,起草后把名字、说明、工具、提示词给用户过目,用户明确同意后才调用 `create`。
- `agent_creator` 技能(内置,与 `skill_creator` 并列,中文):访谈要点(目标、何时用、需要哪些能力、不能做什么)、选工具优先选组、提示词默认以 `${base_prompt}` 开头(在默认上扩展),需要完全替换时再去掉。

### 3.5 工具组

新增内置组 `agents`(默认):`agent_run`、`agent_define`。

## 4. 迁移

| 变更 | 影响 |
|---|---|
| `SessionConfig.agent` 新字段 | 新增可选字段,旧会话配置读出为 None(默认 agent),无需迁移数据 |
| `DefaultAgentRequestRouter` 构造参数加 `session_config_store` | boot、测试同步更新 |
| `AgentRunner.run` 增加 `tool_ceiling`、`parent_session_id` 参数 | 修改现有 ABC(非新增);GatewayAgentRunner、deep_research、测试同步更新 |
| `AgentRequest.tool_ceiling`、`ToolContext.available_tools` 新字段 | 可选字段,默认 None = 不限 |
| AGENT.md 新可选字段 `when_to_use` | 兼容 |

## 5. 验证

- `/agent`:列表、切换后新会话路由到该 agent、旧会话不变、`/clear` 保留绑定、未知 agent 报错。
- `agent_run`:结果回传与 agent_id;`resume` 在同一子会话继续;子 agent 工具 = 自身 ∩ 上限且不含 agent_run;并发上限;父取消联动;失败回传。
- 列表 note:有 `agent_run` 才注入;内容稳定时不重复;工具展开正确。
- `agent_define`:catalog 内容;各项校验;写入后立即可路由 / 可委派;不能提权;不能覆盖内置。
- 全量测试 + compact 回归网(merge_gate 39+1);部署后在 CLI 实测一次委派与一次创建。

## 6. 实验:模型会不会自己委派(2026-09-27,Kimi k2.8)

同一任务跑三种提示词:A = 只有 agent_run 工具描述;B = 系统提示词末尾追加委派引导;C = 委派引导放进可委派列表 note(最终采用)。

| 任务 | A | B | C |
|---|---|---|---|
| 三个 3.4MB 日志分别统计错误(可用 grep 压缩) | 不委派,5 次 Bash,43 秒 | 不委派,5 次 Bash,56 秒 | 不委派,4 次 Bash,20 秒 |
| 6 个网页分别总结并对比(web_open_url 已按目标提炼) | 不委派,61 秒 | 不委派,40 秒 | 不委派,124 秒 |
| 三个 1700–3000 行主循环源码分别写模块说明并对比 | 先自己 9 次 Bash 读,再并行委派 3 个,721 秒;说明写进文件、回答仅摘要 | 1 次 Bash 后并行委派 3 个,287 秒,完整回答 | 1 次 Bash 后并行委派 3 个,207 秒,完整回答 |

结论:
- 能用命令或专用工具压缩的"大"任务,三种都正确地不委派;委派引导没有造成滥用。
- 真正需要大量阅读、且可拆成独立部分的任务,没有引导时模型也会委派,但会先自己读一大圈(重复劳动,用时约 2.5–3.5 倍);有引导时一开始就委派。
- 引导放在 note 与放在系统提示词效果相当,采用 note:只在 agent 可用 agent_run 时出现,不改静态系统提示词。
- 单次运行,未做重复采样;数字只说明方向。
