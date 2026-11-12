# 飞书 View(unified_card_feishu.py)

> 状态机/事件转换/不变量见 [../session-and-card-state.md](../session-and-card-state.md)。本文写飞书 schema 渲染细节与出站媒体消息。

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

## 进度行

回答进行中(streaming),工具面板之后、回答块之前显示一行灰色 markdown `<font color='grey'>› {最近一条进度说明}</font>`;完成 / 取消后不渲染。规则见主规格 §5.5a。

## button(behaviors,不裸 value)

```python
{"tag": "button", "behaviors": [{"type": "callback", "value": {...}}]}
```

裸 `value`(无 behaviors)在 v2 schema 下点击不触发回调。

callback `value` 形式:
- `{"send_text": "<cmd>"}` → 走 send(用户输入回路)
- `{"load_tools": "<card_id>"}` → adapter 本地动作
- `{"cancel": "<run_id>"}` → 按原卡所属 sid/run_id 停止回答

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
| `answer.state=terminal` | 回答正文展开 |
| `answer.state=cancelled` | "✦ 回答已取消" 折叠 |
| `tools_pending>0 & streaming/terminal` | "展开 ✎×N" 按钮 |

## 出站媒体(图片/视频)

卡片之外的独立原生消息,不经 `UnifiedCardState`;实现见 `feishu_api.py` 与 `FeishuAdapter.send_media`。

- 两步:先上传换 key,再发消息。
  - 图片:`POST /open-apis/im/v1/images`(multipart `image_type=message`)→ `image_key`;发 `msg_type=image`,content `{"image_key": ...}`。≤10MB;本实现放行 PNG/JPEG/WEBP/GIF。
  - 视频:`POST /open-apis/im/v1/files`(`file_type=mp4`)→ `file_key`;封面可选,走图片上传;发 `msg_type=media`,content `{"file_key", "image_key"?, "file_name", "duration"}`。仅 MP4,≤30MB;缺省用 ffmpeg 抽首帧作封面,duration 由 ffprobe 读取。
- 发送端点与卡片相同:`POST /open-apis/im/v1/messages?receive_id_type=chat_id`;卡片 `msg_type=interactive`,媒体 `image` / `media`。
- 应用需开启机器人能力,并具备 `im:resource` 上传权限;否则上传阶段返回飞书错误。
- 只由 `send_media` 工具触发,只允许 workspace 内的图片/视频;不提供任意文件,也没有 `msg_type=file` 通路。
- 路由:工具只给 `session_id`,`MediaDelivery` 经 `SessionResolver` 找 `(feishu, chat_id)`;子会话沿 `parent_session_id` 上溯,找不到明确失败,不回退到其他渠道。
