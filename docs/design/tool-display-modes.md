---
title: 工具过程显示模式设计
status: draft(待确认)
date: 2026-09-26
---

# 工具过程显示模式

## 0. 问题与目标

模型现在会一轮一轮地调用工具：搜索几次、打开十几个网页、跑命令、读文件，一次回答可能有几十上百次调用。飞书和 TG 卡片不能把每次调用都铺开。现在的做法是前 3 个显示、其余收进"展开 ✎×N"按钮（§5.4 的 `TOOLS_VISIBLE_LIMIT`），这是"尽量多显示"的思路，调用一多就不好用。

目标：

- **所有工具走同一套显示机制**，不给搜索或任何工具做特殊处理。
- **每个工具自带默认显示配置**：属于哪一类、用什么动词、怎么计数、摘要取哪几个参数。
- **显示模式是一张策略表**，由渠道默认或用户选择；分组、计数、标题与渠道无关。

## 1. 依据

主要参考 DeepSeek Harness（DSH，`~/project/best_agent/deepseek-harness`）和插件 `dsh-view-modes`：

- **核心有四档 transcript view**（compact / standard / detailed / verbose）。每档是一张策略表：完成的轮次是否折叠、步骤怎么分组、运行中是否显示详情等。渲染器只读策略字段，不判断模式名，加模式只改表（`client/ui-chat/src/client/presentation-policy.ts:24-53`）。
- **分组是纯数据步骤，和模式无关**：用户输入、可见正文是硬分界；思考并入相邻过程组；其余调用都进组（`conversation-nodes/process-groups.ts:119-171`）。
- **插件的三种模式**：
  - Verbose：完整的每一步。
  - Normal：按组折叠，标题形如"运行了 2 条命令，思考了 3 次，阅读了 2 个文件"，可展开。
  - Summary：运行中只有一行动态状态（动词 + 详情 + 计时 + 步数）；完成后整轮折叠成回答上方的一段回顾。
- **组标题**：插件逐项带计数；核心取前 3 类、不带计数，用"并"连接。运行中组标题是"正在运行命令 · 详情"，至少停留 150ms 再切换。
- **错误和中断永远不折叠**：折叠后的摘要换成错误结果的第一行（`turn-process.ts:162-167`）。
- **DSH 的缺陷**：工具分类是 UI 层按工具名写死的表（插件、核心各一份），`todo_write` 被误判成"修改了文件"，新工具都落进"其他"。工具虽然能声明 `presentCall`，Web 和 ACP 都没用上。**所以本设计把显示配置放到工具定义里，UI 层的名字表只做兜底。**

Claude Code 与 Codex 的 TUI 也是"每次调用一行 + 结果折叠"，把汇总计数放在状态区。Kimi Web 在对话流里把连续的工具调用折叠成一句摘要，失败的附上"（N 失败）"。

## 2. 工具的显示配置（工具契约）

`ToolSpec` 新增可选字段 `display`，定义在 `tools/contracts.py`：

```python
ToolCategory = Literal["read", "search_code", "edit", "execute", "web_search", "web_open",
                       "research", "memory", "plan", "delegate", "ask", "other"]

@dataclass(frozen=True)
class ToolDisplay:
    category: ToolCategory
    verb: str                     # 完成态动词,如 "搜索网页"、"打开"
    unit: str = "次"              # 计数单位,如 "次"、"个网页"、"条命令"、"个文件"
    count_arg: str | None = None  # 按某个数组参数的长度计数(如 urls → 打开 5 个网页);None = 每次调用计 1
    summary_args: tuple[str, ...] = ()   # 单行摘要取哪些参数(按优先级)
    result: Literal["none", "preview", "full"] = "preview"   # 展开后显示多少结果
```

- 没有声明 `display` 的工具（MCP、扩展工具）：由渲染层兜底，分类 `other`，动词用工具名，摘要沿用现在的参数优先级（`description > path/... > 第一个 kv`）。
- 工具不必知道渠道。进行时的文案由类别决定（"正在搜索网页"、"正在打开网页"），放在渲染层的文案表里；工具只给完成态的动词和单位。

现有工具的默认配置：

| 工具 | category | verb | unit / count_arg | summary_args |
|---|---|---|---|---|
| bash | execute | 运行 | 条命令 | description, command |
| read | read | 读取 | 个文件 | file_path, path |
| write / edit | edit | 修改 | 个文件 | file_path, path |
| glob / grep | search_code | 搜索代码 | 次 | pattern, path |
| web_search | web_search | 搜索网页 | 次 | queries |
| web_open_url | web_open | 打开 | 个网页 / urls | urls, objective |
| deep_research | research | 深度研究 | 次 | question, job_id |
| memory_search / memory_write | memory | 查记忆 / 写记忆 | 次 | query, title |
| todo_write | plan | 更新清单 | 次 | — |
| skill_load / tool_search | other | 加载技能 / 加载工具 | 次 | name, query |
| cron / report_goal / enter_plan_mode / enter_worktree | other | 各自动词 | 次 | — |

## 3. 过程组（与模式无关的纯数据）

harness 在 `tool_started` 事件的 payload 里带上该工具的 `display`（harness 执行前本来就要取工具对象）。渲染层据此把回答区的过程归并成**过程组**：

```python
@dataclass
class ProcessGroup:
    caption: str                   # 本组开头模型写的进度说明(progress_update),可为空
    members: tuple[ToolBlock, ...] # 每次调用:动词 + 摘要 + 耗时 + 状态 + 结果预览
    counts: dict[str, int]         # 按 (category) 累计:次数或 count_arg 的长度之和
    thinking: int                  # 本组内的思考段数
    running: str = ""              # 运行中的调用:"正在打开网页 · 5/12"(tool_progress 实时更新)
    failed: int = 0
    closed: bool = False
```

分组规则（照 DSH 核心）：

1. **模型写出的文字是边界**。与工具调用同一轮的文字（`progress_update`，如"我先搜一下这份研报"）开启一个新组，并作为该组的 `caption`。这是模型自己对这一步的说明，比按工具类型拼出来的标题更准确。最终回答文字结束过程区。
2. 思考并入当前组（只计数）。
3. 工具调用进当前组，按类别累计计数。
4. `tool_progress` 只更新该调用的运行中详情（如"正在阅读 5/12 个网页"），不新增成员。
5. 整轮累计 = 各组 `counts` 之和，用于回顾行。

组标题生成（中文，逐项带计数，照插件；类别按首次出现顺序，最多 4 项，其余计入"等 N 项"）：

```
搜索网页 3 次，打开 12 个网页，思考 2 次
运行 2 条命令，修改 3 个文件
```

## 4. 显示模式（策略表）

| 策略字段 | detailed（详细） | grouped（分组） | summary（摘要） |
|---|---|---|---|
| 过程区单位 | 每次调用一个面板 | 每组一个面板 | 整轮一行 |
| 面板标题 | `✎ 工具 · 摘要 · 耗时 · 状态`（现状） | `caption` 或组标题 | 运行中:动态状态;完成后:回顾行 |
| 展开后内容 | 调用结果（按 `result` 截断） | 成员逐行:动词 + 摘要 + 耗时 + 状态 | 分组视图(同 grouped) |
| 运行中详情 | 工具面板内 | 最后一组标题后追加 `· 正在…` | 进度行:`› 正在打开网页 · 5/12 · 已用 1:23 · 14 步` |
| 完成后 | 保持 | 各组折叠 | 回答上方一行:`搜索网页 5 次，打开 23 个网页，运行 2 条命令 · 用时 2 分 10 秒` |
| 数量上限 | 现有 3 个 + 展开按钮 | 最多显示 N 组(默认 6),更早的合并成一行 | 无 |
| 错误 | 展开 | **失败的调用单独一行,不折叠**,摘要换成错误第一行 | 回顾行后附"（N 次失败）",失败调用逐行列出 |

- **detailed** 保留现在的行为，CLI 默认使用。
- **grouped** 是飞书和 TG 的默认模式。
- **summary** 适合只关心结果的场景，也可作为移动端默认。
- 渲染器只读策略字段，不判断模式名。以后加模式（例如只显示回答的 minimal）只改这张表。
- 标题更新节流沿用各渠道已有的卡片更新节流；同一标题至少停留一个节流周期。

模式选择：渠道给默认值；用户可以按会话切换（如 `/view detailed|grouped|summary`），存在 `SessionConfig`。

## 5. 渠道渲染

| | 飞书 | TG | CLI |
|---|---|---|---|
| 组面板 | `collapsible_panel`,标题 = 组标题,body = 成员行 | expandable blockquote,首行组标题 | Textual Collapsible |
| 回顾行 | 灰色一行,点开看分组 | 斜体一行 + 展开按钮 | 暗色一行 |
| 失败行 | 红色 | `✗` 前缀 | 红色 |
| 单卡长度 | 过程区不再随调用数增长,只随组数增长 | 同左 | 不限 |

## 6. 影响与迁移

| 项 | 变化 |
|---|---|
| 工具契约 | `ToolSpec.display` 新增可选字段(不破坏现有工具);内置工具补齐声明 |
| harness 事件 | `tool_started` payload 增加 `display`(类别、动词、单位、计数) |
| 卡片模型 | `AnswerSection` 增加 `groups`;`tools` / `tools_pending` / `load_pending_tools` 只在 detailed 模式使用 |
| 卡片规格 | `session-and-card-state.md` §4.2 顺序第 4 项、§5.4 工具下拉、§7 差异表、I8 不变量需要更新(**改规格,需确认**) |
| SessionConfig | 新增 `view_mode`(可选;渠道默认值在 adapter) |
| 回归网 | merge_gate 里涉及工具面板的用例只在 detailed 模式下保持原断言;grouped/summary 新增用例 |

## 7. 实施阶段

1. 契约：`ToolDisplay` + 内置工具声明 + `tool_started` 带 `display`。
2. ViewModel：`apply_event` 归并过程组、累计计数、生成标题（纯函数，单测覆盖）。
3. 渲染：飞书 / TG / CLI 三种模式；默认值飞书、TG 为 grouped，CLI 为 detailed。
4. 会话切换命令与 `SessionConfig.view_mode`。
5. 实测：用大摩研报问题（多轮 web_search + web_open_url）检查三种模式下的卡片。

## 8. 待确认

1. 飞书 / TG 默认用 grouped，还是 summary？
2. 进度说明（`progress_update`）作为组边界和组标题：同意吗？
3. 模式切换命令名 `/view`，存 `SessionConfig.view_mode`。
4. §6 所列卡片规格的修改。
