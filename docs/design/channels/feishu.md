# 飞书 View(unified_card_feishu.py)

> 状态机/事件转换/不变量见 [../session-and-card-state.md](../session-and-card-state.md)。本文只写飞书 schema 渲染细节。

## 输出形态

`render(state: UnifiedCardState) -> dict` 直接喂 lark `send_message` / `patch_message`,顶层永远:
```python
{"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": [...]}}
```

## Section 顺序(固定)

`top_buttons → compact → answer → menu → bottom_buttons → footer`

详见 [unified_card_feishu.py:render](../../../src/xana/agent_adapter/channels/unified_card_feishu.py)。

## panel(分块面板)

所有可折叠面板统一走 `_panel(title, body, folded)`:
- `tag: collapsible_panel`
- `background_color: "grey-50"` —— **灰底是对的,不要再改**
- header `title` 用 `plain_text`(不是 markdown,避免 ![](url) 被解析成 image_key)
- header 右侧 `standard_icon: down-bold_outlined`,展开旋转 -180°
- body `_sanitize_md`:把 `![alt](url)` 替换成 `[图片: url]`,避开飞书 200570(invalid image keys)

## button(behaviors,不裸 value)

```python
{"tag": "button", "behaviors": [{"type": "callback", "value": {...}}]}
```

裸 `value`(无 behaviors)在 v2 schema 下点击不触发回调。

callback `value` 形式:
- `{"send_text": "<cmd>"}` → 走 send(用户输入回路)
- `{"load_tools": "<card_id>"}` → adapter 本地动作
- `{"detach": "<run_id>"}` → 后台

## 容量限制

- 飞书卡 ~30KB 总上限(200800 universal fail)
- 单 tool body 截 800 字符
- thinking / answer 不截(由 model 决定)
- 多 tool 全 body 会超限 → patch 400 → UI 卡住 → 严格遵守 800

## 状态映射

| UnifiedCardState | 飞书呈现 |
|---|---|
| `compact.state=input/streaming` | 一行 "▰…▱ 压缩中" + 折叠面板"压缩中" |
| `compact.state=completed` | 一行 "▰▰▰▰▰▰▰▰▰▰ 完成压缩" + 折叠面板"已压缩 N 条消息" |
| `compact.state=cancelled` | "压缩已取消" + 折叠面板 |
| `answer.state=streaming` | thinking 折叠 / tools 折叠 / "✦ 回答中" 展开 |
| `answer.state=detached` | 所有面板折叠 / "✦ 回答" 折叠 |
| `answer.state=terminal` | 默认展开;`terminal_from_bg=True` → 折叠 |
| `answer.state=cancelled` | "✦ 回答已取消" 折叠 |
| `tools_pending>0 & streaming/terminal` | "展开 ✎×N" 按钮 |
| `tools_pending>0 & detached` | "_后台 ✎ 中_" 静态 md(不可点) |
