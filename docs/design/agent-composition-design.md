---
title: agent 组合:工具组、工具选择表达式、提示词覆盖
status: active
date: 2026-09-27
---

# agent 组合:工具组、工具选择表达式、提示词覆盖

## 1. 目标

让新建 agent 的决策成本低、自由度高:

- 专用工具(例如以后给迭代智能体做的工具)不会默认发给聊天 agent;
- 可以创建一个零工具的 agent,也可以只给几组工具;
- 系统级工具也能整体禁用,只是默认注册一部分;
- 提示词可以完全覆盖,也可以在默认提示词上扩展;
- 为以后的 agent_creator 铺路:模型从一份简短的"工具组目录"里选 1–3 组,而不是在二十多个工具里逐个挑。

不在本次范围:容器读写后端(按 agent 绑定执行后端)、agent_creator、`/agent` 路由。

## 2. 参考

| 来源 | 工具选择 | 提示词 |
|---|---|---|
| DSH preset(`best_agent/deepseek-harness/packages/preset/agent-preset`) | preset 显式列出完整插件清单,无继承、无"默认全部";相关插件打成组(planning / compaction / delegation);新建时复制现成 preset(minimal 最短)改;创造模式里模型用 `cordis_inspect` 查可用包 | persona 插件 prefix / suffix |
| Kimi Code(`docs/zh/customization/agents.md`) | 不写 `tools` = 全部,`[]` = 全不给,名单;`disallowedTools` 之后再减;写错工具名告警 | 正文即系统提示词;`${base_prompt}` 嵌入默认提示词(包裹),不放则整体替换 |
| Claude Code | 不写 tools = 继承全部 | 正文替换 |

取舍:Kimi / CC 的"不写 = 全部"建立在工具都是通用编码工具之上;xana 会有专用工具,因此改用 DSH 的"分组 + 默认组合"。提示词沿用 Kimi 的 `${base_prompt}`。

## 3. 设计

### 3.1 工具目录与工具组

- 系统中注册的全部工具构成**工具目录**(`ToolRegistry`)。
- `ToolSpec.toolset: str` 声明工具所属的组;`""` 表示未分组。
- 组的定义 `Toolset(name, description, default)`(`tools/contracts.py`)登记在工具目录里:`ToolRegistry.register_toolset()` / `list_toolsets()`。内置组的权威表在 `tools/toolsets.py`,由 `boot()` 登记;附加项注册工具时同时登记自己的组并决定是否默认。
- 内置组:

| 组 | 工具 | 默认 |
|---|---|---|
| fs | Read / Write / Edit / Glob / Grep | ✓ |
| exec | Bash | ✓ |
| web | web_search / web_open_url / web_fetch | ✓ |
| memory | memory_search / memory_write | ✓ |
| session | TodoWrite / EnterPlanMode / EnterWorktree / report_goal | ✓ |
| meta | tool_search / skill_load | ✓ |
| research | deep_research / background_research | ✓ |
| schedule | CronCreate / CronList / CronDelete | ✓ |

- 未分组或所属组未登记的工具不属于任何组合,只能按工具名或 `*` 选中;boot 对"所属组未登记"记 warning。

### 3.2 系统级禁用

`system.json`:

```json
"tools": { "disabled": ["exec", "memory_write"] }
```

条目可以是组名或工具名。`boot()` 统一经一个注册函数注册工具,被禁用的工具不进入工具目录(对所有 agent 都不存在)。默认为空,即全部注册。

### 3.3 agent 的工具选择表达式

`AgentSpec.tools: tuple[str, ...] = ("default",)`,取代 `allowed_tools`。元素可以是:

| 元素 | 含义 |
|---|---|
| `default` | 所有 `default=True` 的组里的工具 |
| 组名 | 该组全部工具 |
| 工具名 | 该工具 |
| `*` | 工具目录里的全部工具 |

- `()` = 零工具。
- 结果再减去 `AgentSpec.disallowed_tools`(同样接受组名和工具名)。
- 例:聊天 `("default",)`;迭代智能体 `("default", "evolution")`;researcher `("web", "Read")`;纯净 agent `()`。
- 解析是纯函数 `resolve_tools(selection, disallowed, specs, toolsets) -> frozenset[str]`(`tools/toolsets.py`)。harness 每次 invoke 用工具目录与扩展工具的 spec 解析一次,schema 过滤与执行前校验都用这个结果(替代 `_tool_allowed`)。
- `HarnessContext.config_snapshot` 里没有 `tools` 键时(直接使用 harness、不经 agent 层)不过滤:调用方给的工具目录本身就是范围。
- 表达式里写了不存在的组名或工具名:boot 记 warning(沿用 B2 的检查)。

AGENT.md:`tools` 改为可选,不写 = `default`,`tools: []` = 零工具;新增可选 `disallowed_tools`。

### 3.4 提示词

`AgentSpec.system_prompt: str | None`,取代 `identity` + `system_prompt` 两个字段:

- `None`:xana 默认系统提示词。
- 字符串:它就是系统提示词模板。占位符:
  - `${base_prompt}`:嵌入完整的默认系统提示词(含环境段与扩展片段);
  - `${env}`:环境段;
  - `${extensions}`:扩展贡献的 system 片段。
  - 不放任何占位符 = 完全替换,除 PromptProfile 尾段(agent 显式配置的会话开关)外不追加任何内容。
- 未知占位符原样保留。
- AGENT.md 正文即 `system_prompt`。agent_creator 以后给出的模板默认以 `${base_prompt}` 开头(在默认上扩展),删掉即完全替换。
- 内置 researcher 迁移为 `"${base_prompt}\n\n<研究员身份>"`。

### 3.5 每轮注入跟随工具与占位符

不新增注入开关,每项注入绑定到它依赖的东西:

| 注入 | 条件 |
|---|---|
| 技能列表(SkillsListingContributor) | agent 可用 `skill_load`(`TurnInputContext.available_tools` 新字段,贡献者自行判断) |
| 按需工具清单 / defer | agent 可用 `tool_search`;不可用时不隐藏任何工具,全部给完整 schema |
| todo 提醒 | agent 可用 `TodoWrite` |
| 记忆召回 | agent 可用 memory 组的任一工具;提示"用 Read 查看全文"仅在可用 Read 时出现 |
| 环境段 | 默认提示词,或模板含 `${env}` / `${base_prompt}` |
| 续跑类(只思考无输出、输出截断、无进度提醒) | 始终保留:属于循环机制,不是上下文 |

"零工具 + 不含占位符的自写提示词"因此自然得到不注入任何上下文的纯净 agent。

注:todo / tool_search / skill_load 的工具名判断目前写在 harness 里,与 review 中"附加项迁出主循环(Contributor 化)"一并处理,本次不扩大范围。

## 4. 迁移

破坏性变更均为原位替换,不留兼容写法:

| 旧 | 新 | 调用方 |
|---|---|---|
| `AgentSpec.allowed_tools`(空 = 不限制) | `AgentSpec.tools`(默认 `("default",)`) | chat_agent、researcher、agents/registry.py、测试 |
| `AgentSpec.identity` + `system_prompt` | `system_prompt` 模板 | chat_agent、researcher、agents/registry.py、harness `_session_system`、测试 |
| snapshot `allowed_tools` / `agent_identity` / `agent_system_prompt` | snapshot `tools` / `disallowed_tools` / `agent_system_prompt` | chat_agent → harness |
| AGENT.md `tools` 必填非空 | 可选;`[]` = 零工具 | markdown_loader、测试 |
| `SubagentRequest.allowed_tools`(agents/contracts.py,无生产调用方) | 不动,随空 ABC / 死代码清理另议 | — |

持久数据:`~/.xana/agents` 当前不存在,无 AGENT.md 需要迁移;SessionConfig 不涉及。

## 5. 验证

- `resolve_tools` 单测:default / 组名 / 工具名 / `*` / `()` / disallowed 组名 / 未分组工具。
- harness:零工具 agent 的请求 schema 为空、无技能列表 / 记忆召回 / 按需工具清单;执行前校验拒绝范围外工具。
- 提示词:`${base_prompt}` 包裹、完全替换(无环境段)、`${env}` 单独放置。
- boot:`tools.disabled` 按组名 / 工具名生效;未登记组与写错名字告警;默认聊天的工具集合与改动前一致。
- 全量测试 + compact 回归网(merge_gate 39+1)。
