# CLI View(unified_card_cli.py + xanac Textual app)

> 状态机/事件转换/不变量见 [../session-and-card-state.md](../session-and-card-state.md)。本文只写 CLI(xanac Textual REPL)的渲染细节。

## 形态

`render(state: UnifiedCardState) -> list[Widget]`,返回顶到底 Textual widget 列表;
宿主 `XanacApp` 调 `_set_body(widgets)` → `remove_children + mount` 替换 fg-pane 子树。

## widget 映射

| Section | Widget |
|---|---|
| compact 一行进度 | `Static` |
| compact 折叠 | `Collapsible(Static(body))` |
| thinking | `Collapsible` |
| tool block(≤3 visible) | `Collapsible` |
| 工具展开按钮 | `Horizontal(CardButton(spec))` |
| 进度行(§5.5a,进行中) | `Static("[dim]› …[/dim]")`,完成后不渲染 |
| answer | `Static(markup)`,标题 + 截 12k 字符 |
| menu | `Markdown` |
| top / bottom buttons | `Horizontal(CardButton, ...)` |
| footer | `Static` (dim italic) |

## 按钮路由(`XanacApp.on_button_pressed`)

`CardButton.spec` 三种形态:
- `spec.send` → 作为用户输入 submit(gateway.submit) —— 走 /cancel /attach 等命令
- `spec.action == "load_tools"` → 本地动作(adapter 实现 reveal)
- 其他(action=detach 或默认)→ `gateway.detach(session_id, run_id)`

## tab 面板

侧边 ListView 显示 bg run 列表;`🟡 ` 前缀表示 frozen。
Tab 切焦点,Enter 选中 → `gateway.attach(session_id, run_id)`(对应 master §7.4)。

## 限制

- thinking ≤ 4000 字符
- 单 tool body ≤ 2000 字符
- answer ≤ 12000 字符(超出 `…(已折叠)`)
- visible tools ≤ 3,超出 → "展开 ✎×N" 按钮

## 与飞书/TG 的差异

- 工具溢出提示 = **按钮**(同飞书,不同 TG)
- 无 markdown 截断特殊处理(Static 直接吃,Textual 容错)
- 无 expand_mode 全屏翻页(TG 特性)
