# TG View(unified_card_telegram.py)

> 状态机/事件转换/不变量见 [../session-and-card-state.md](../session-and-card-state.md)。本文只写 TG 渲染细节。

## 输出形态

单条 message:
- `text`: MarkdownV2(telegramify-markdown 转义)
- `reply_markup`: inline_keyboard(top + bottom buttons)
- `parse_mode`: MarkdownV2
- 长 streaming 采用 edit_message_text(同 message_id),不重发

## Section 顺序(固定,同飞书)

`top_buttons → compact → answer → menu → bottom_buttons → footer`

## 可折叠区块

TG 用 **expandable blockquote**(`**>` 块前缀,首行 `||` 包裹)模拟飞书的 collapsible_panel:
- thinking / tools body / compact summary 都用 expandable blockquote
- 进度行(§5.5a):回答进行中,在分割线之前显示斜体一行 `_› {最近一条进度说明}_`;完成 / 取消后不渲染。
- 回答块**无标题、不折叠**:`━━━━━━━ ✦ ━━━━━━━` 分割线后接正文(markdown);cancelled 显示 "_回答已取消_"。
  不折叠的原因:折叠会把回答的 markdown 套进 blockquote,出现嵌套折叠。

## expand_mode(TG only,飞书忽略)

TG 在卡底部比飞书多 **3 个按钮**:"展开 思考 / 展开 工具 / 展开 答案"(全量,非 ✎×N 那种局部)。点击进入全屏单段卡:

`state.expand_mode in {default, thinking, tools, answer}`:
- `default`: 正常分块
- `thinking/tools/answer`: 全屏单段,带翻页(`expand_page`)
  - **tools 固定一页 10 个**(按数量均分,避免按长度切出不均匀的页)
  - answer 按 TG 单消息长度切页

## answer markdown 不闭合守门

streaming 时尾部容易切到未闭合元素(`*x` 缺 `*`、表格半行、未闭 ``` 等)。
View 层走 telegramify-markdown 容错;必要时截到安全 boundary 或补回闭合符。

## linkify_commands

`state.linkify_commands=True` 时,正文 `/cmd` 变成 `<a href="...">` 原生可点 →
可以隐藏 bottom_buttons(节省空间)。

## 状态映射

| UnifiedCardState | TG 呈现 |
|---|---|
| `compact.streaming` | 一行进度 + expandable "压缩中..." |
| `compact.completed` | "▰…▰ 完成压缩" + expandable "已压缩 N 条消息" |
| `answer.streaming` | thinking 折叠 / tools 折叠 / 分割线后回答正文可见 |
| `answer.detached` | thinking/tools 折叠,回答正文保持可见(不折叠)+ footer "🟡 正在后台回答" |
| `answer.terminal` | 回答正文可见(含 `terminal_from_bg=True`,不折叠) |
| `answer.cancelled` | 分割线后 "_回答已取消_" |
| `tools_pending>0` | 文字提示 "其他 ✎×N,点下方展开工具查看"(非按钮);全量查看走底部 "展开工具" |

## 限制

- TG message 4096 字符上限
- 超长 answer 分页(`AnswerSection.page`),top buttons 出 "下一页"
