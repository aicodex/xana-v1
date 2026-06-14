# 卡片内容布局 v2 —— tail-3 工具 + 3 块结构 + 工具结果默认折叠

> 下一阶段。**独立** spec —— 不与 `2026-06-14-async-chat-design.md`(PH-first + 双卡 = run 维度)
> 或 `ASYNC_RUN_DESIGN.md`(主上下文/PH/写队列)重合。前两者管"什么时候出哪张卡",本 spec 管
> "每张卡内部画什么"。
>
> 状态: 草案 / 待规划

---

## 0. 动机

当前 fg streaming 卡随事件递增 —— 思考全展开、每个工具调用一张折叠面板,几十个工具下来卡片很长,
界面跳得厉害。用户希望模仿 CC TUI 的紧凑显示。

## 1. 设计

### 1.1 卡片三块结构

```
┌─────────────────────────────────────┐
│ [fg streaming 卡 顶部控制:⏹ ➡]    │  ← 来自 async-chat-design.md §2.1,不变
├─────────────────────────────────────┤
│ 💭 思考 (折叠,默认收起)            │  block 1: thinking
├─────────────────────────────────────┤
│ 🔧 工具活动 (tail 3,实时刷新)      │  block 2: tools (tail-N)
│   • 📖 read_file foo.py            │
│   • 🔍 grep "pattern" src/         │
│   • ⏳ write_file bar.py …         │
├─────────────────────────────────────┤
│ 💬 回答 (markdown)                  │  block 3: answer
└─────────────────────────────────────┘
```

无论这一 turn 触发了几次工具调用,工具块只显示**最近 3 次**(tail 3),每次只显示一行
摘要:`{icon} {tool_name} {args 摘要}`,**不显示返回值**。早期工具调用从可见区域滑出。

### 1.2 默认折叠/展开

| 块 | 默认 | 备注 |
|---|---|---|
| 思考 | 折叠 | 与现状一致 |
| 工具(tail 3) | 展开(行级摘要) | 但无返回值 |
| 回答 | 展开 | 与现状一致 |

行级摘要例子:`📖 Read · src/foo.py:120-180` —— 一行,无参数 JSON,无 result。

### 1.3 看完整工具历史 → 菜单触发

`/tools` 命令(或某个固定按钮 `🔧 工具历史`):发出一张**工具历史卡**(独立短卡,不进 turn 状态机,
和 `/attach` 菜单类似):

- 标题: `🔧 工具历史 · {N} 次调用`
- body: 每个工具一个 `collapsible_panel`(折叠),内含完整 args(JSON)+ 完整 result(markdown)。
- 与 fg 流卡解耦 —— 工具历史卡是只读的快照。

### 1.4 行级摘要格式约定

| tool_name | 摘要模板 |
|---|---|
| Read | `📖 Read · {path}{:start-end if given}` |
| Edit | `✏️ Edit · {path}` |
| Write | `📝 Write · {path}` |
| Grep | `🔍 Grep · {pattern}{ in path}` |
| Glob | `🗂 Glob · {pattern}` |
| Bash | `⌨ Bash · {first 40 chars of command}` |
| Task / Agent | `🤖 Agent · {description}` |
| WebFetch | `🌐 Fetch · {url host}` |
| WebSearch | `🔎 Search · {query}` |
| TodoWrite | `☑ Todos · {N} items` |
| 其他 | `🔧 {tool_name} · …` |

进行中:`⏳ {tool_name} · {args 摘要}`。完成 ok:用 tool 自己的 icon。完成 fail:`✖ {tool_name} · …`。

## 2. 数据模型影响

`TurnDraft.tool_blocks` 现存的就是按调用顺序追加的 `ToolBlock` 列表 —— 渲染时改成:
- block 2 只取 `tool_blocks[-3:]`,每个用 `_tool_summary(blk)` 渲染一行(新 helper),不展开 args/result。
- `/tools` 菜单卡: 全量遍历 `tool_blocks`,每个走 `_tool_panel(blk)`(现有 helper,带 JSON + result 折叠)。

`TurnDraft` 不改字段。新增的只是渲染层的两个 helper:
- `_tool_summary(blk) -> dict` → 一行 markdown 元素
- 已有 `_tool_panel(blk)` 复用给历史卡

## 3. 飞书 / TG / CLI 落地

### 3.1 飞书
- `build_fg_streaming_card`:body 改为 [思考折叠 (现有) | 工具 tail-3 list (新) | 回答 markdown (现有)]
- `build_tools_history_card(tool_blocks)`:历史卡构造器,完整 `_tool_panel` 列表
- `/tools` 命令(commands.py)→ payload.tool_history_choices? 或直接走特殊 event 路径,feishu.render 早分支送历史卡

### 3.2 Telegram
- `render_fg_text` 单 message HTML,工具块改为 tail-3 行(用 `\n• ...` markdown)
- `/tools` 同样独立 message 编辑(reply_markup=None,长 markdown 折叠用 `<details>`)

### 3.3 CLI / Textual
- StdoutRenderer: 工具块只 print 摘要行(tail-3),完整结果用 `/tools` 拉。
- TextualRenderer: 工具 pane 也是 tail-3,有键绑切到工具历史面板。

## 4. 与 async-chat-design 的关系

- `async-chat-design.md` 管:fg 卡 vs bg 卡的状态机,detach/attach/PH 替换。
- 本 spec 管:每张 fg 卡内部 thinking/tools/answer 的布局。
- bg 占位折叠卡(spec §2.1 [B])里的 collapsible_panel 内容,也应改用本 spec 的 3 块 markdown(tail-3 工具 + 答案 + 思考片段),保持视觉一致。
- 不冲突。可以单独排期。

## 5. 任务分组 (待 brainstorming → writing-plans)

```
T1 — 共享渲染层
  - TurnDraft 新增 _tool_summary helper (无字段改动)
  - feishu_card: build_fg_streaming_card body 改 tail-3
  - feishu_card: build_tools_history_card 新增
  - bg 占位卡 panel_md 也走相同 helper(保持一致)
T2 — /tools 命令
  - DefaultCommandRouter 注册 /tools (triggers_turn=False, payload.tool_history=[...])
  - feishu / telegram render 早分支:tool_history 事件 → 独立短卡 / message
T3 — Telegram + CLI 适配
  - render_fg_text / StdoutRenderer / TextualRenderer 同步 3 块结构
T4 — 测试
  - 单测 tail-3 截断
  - 单测 /tools 历史卡内容完整
  - dual-card scenarios 复测无回退
T5 — 飞书 / TG 手测验收
T6 — 沉淀到 ASYNC_CHAT_DESIGN.md §2.1.A.body 引用本 spec
```

## 6. 不在范围

- 思考/工具/回答 的具体折叠图标 / 颜色 / 字号 micro-tuning — 视觉走查时再调。
- CC TUI 那种 streaming spinner / 颜色 ANSI 序列 — 飞书 / TG 卡片做不到,只在 CLI 做。
- 历史卡的滚动 / 分页 — 一次出完(<6 工具)或加 `已截断 N 条…` 文末(>20 工具)。

## 7. 风险

- TG 单 message HTML 长度限制(4096 chars):tail-3 后还有可能炸 → 答案块超长时强制截到 3000 字 + "…(完整见 /tools)"。
- 飞书 collapsible_panel 嵌套层数 / 性能:实测一张卡几十个面板时编辑延迟会上去 → 本 spec 主流卡 ≤5 元素,只有 /tools 历史卡可能有 N 个面板;OK。
- 用户已经习惯了"工具结果直接展开看",改默认折叠后第一次会困惑 —— 顶部加一行小字 `工具结果默认折叠,/tools 看详情`。

---

> brainstorming 完进 writing-plans → subagent-driven。等当前 async-chat 收尾(M 验收 + N 沉淀)后再排期。
