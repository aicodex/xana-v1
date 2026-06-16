# Card 接入指南(给新 agent / 新 channel)

> 你想让 agent 通过 xana 在飞书 / TG / CLI 渲染一张交互卡片。本文告诉你接哪个口、发什么、不用关心什么。

---

## 1. 你要写什么 / 不用写什么

### 你要写

- 一个 `Agent`(实现 `xana.agents.interfaces.Agent`),`send(request)` yield `AgentEvent`
- 可选:工具(`xana.tools.interfaces.Tool`)

### 你**不用**写

- ❌ 卡片 HTML / 飞书 schema / TG MarkdownV2
- ❌ 按钮回调路由、翻页、展开 / 折叠
- ❌ 流式节流、消息边界 escape、长度截断
- ❌ /clear、/cancel、/attach、/compact、/help 等命令(都在 command_router)

这些 channel adapter 都帮你做了。**你只发事件,卡片自己长出来。**

---

## 2. 数据模型(MVVM)

**M(Model)— 平台无关,3 端共享**:`UnifiedCardState`(`unified_card.py`)

```python
@dataclass
class UnifiedCardState:
    card_id: str               # 通常 = run_id
    ph_run_id: str | None
    compact: CompactSection | None     # /compact 进度条 + 归档摘要
    answer: AnswerSection | None       # thinking + tools + answer body
    menu: MenuSection | None           # /help /clear 等命令结果文本
    footer: FooterText                 # 状态条
    top_buttons: tuple[ButtonRow, ...]  # 顶部行(取消回答 / 后台回答)
    bottom_buttons: tuple[ButtonRow, ...]  # 底部行(/help 子命令、attach 列表)
    detached: bool             # 是否后台
    terminal: bool             # 是否终态
    terminal_from_bg: bool     # 后台回写终态(影响默认折叠)
    expand_mode: Literal["default", "thinking", "tools", "answer"]  # TG 专用全屏
    expand_page: int           # expand 翻页
    linkify_commands: bool     # TG 用:正文 /cmd 可点 → 隐 bottom_buttons
```

**VM(ViewModel)— 3 端共享**:`Card`(`card.py`)
- 拥有 `state: UnifiedCardState`
- `dispatch(event)` 收事件 → 内部 `apply_event` 改 state → 渲染节流
- `reveal_tools() / set_expand() / freeze() / terminalize()` 等显式动作
- 通过 `bind_transport(deliver, task_sink)` 注入平台交付函数

**V(View)— 每平台一个**:
| 平台 | 文件 | 输出 |
|---|---|---|
| 飞书 | `unified_card_feishu.py::render(state)` | lark v2 card schema dict |
| TG | `unified_card_telegram.py::render(state)` | `(markdown_v2_text, inline_keyboard)` |
| CLI | (待实现) | textual 控件树 |

**关键约束**:View **只读** state。不能改。状态机改动一律在 events / Card 方法里。

---

## 3. 发送事件 = 让卡片动起来

你的 agent `send(request)` yield `AgentEvent`。这些事件 → channel adapter `render(event)` → `card.dispatch(event)` → renderer 重画。

### 3.1 全套事件清单

| 事件 kind | payload 字段 | 效果 |
|---|---|---|
| `turn_started` | — | 新建 answer section,top_buttons 出现"取消回答 / 后台回答" |
| `model_delta` | `text`, `channel` ("thinking" 或 "answer") | 流式追加到 thinking 或 answer body |
| `tool_started` | `tool_name`, `call_id`, `input` (dict) | 加 tool block(头部 `Read · /file.py`),前 3 个显示,多余进 buffer |
| `tool_finished` | `tool_name`, `call_id`, `ok`, `text`, `elapsed_ms`, `cancelled` | 更新 tool body + 标题状态(`· 20ms · ok` / `fail` / `cancel`) |
| `final` | `text` | 回答终态,footer 🟢 |
| `error` | `text` | 终态,footer 🟢(可改红) |
| `cancelled` | `reason` | 终态,footer 🔴 |
| `paused` | — | 暂时不动(预留) |

### 3.2 进度事件(ProgressEvent — gateway/command 发,你一般不发)

| kind | payload | 效果 |
|---|---|---|
| `run_started` | `model`, `label` | 卡片初始化(adapter 已自动) |
| `run_detached` | — | 卡片折叠 / 冻结(转后台) |
| `ph_replaced` | `text`, `ctx_used`, `elapsed_s`, `cancelled` | 后台 run 结束 → 终态 |
| `run_completed_bg` | — | 兜底终态信号 |
| `usage_progress` | `model`, `tokens_in`, `context_window`, `elapsed_s` | footer 实时更新 model + ctx + 用时 |
| `compact_started` / `compact_progress` / `compact_finalized` | — | /compact 进度段 |
| `menu_response` | `specs` | /help 列表菜单卡 |
| `attach_options` | `candidates` | /attach 列表菜单卡 |
| `command_progress` | `text` | 命令执行中占位(menu_label 计时) |

`usage_progress` 你**必须发**(harness 已经在每次模型 usage 时自动发了),否则 footer 三件套(model+ctx+用时)出不来。

---

## 4. 平台差异(哪些是分开的)

| 维度 | 飞书 | TG | CLI(待做) |
|---|---|---|---|
| 字数预算 | ~30KB | 4096(硬上限) | 终端宽度 |
| 折叠 | `collapsible_panel` 原生 | `**>...||` MarkdownV2 expandable blockquote | textual `Collapsible` |
| 按钮 | 顶/中/底任意位置 | inline keyboard 集中底部 | textual `Button` |
| Markdown | 飞书自己渲(注意 `![](...)` 会触发 image_key 校验)| `telegramify-markdown` → MarkdownV2 | rich markdown |
| 翻页 / 全屏展开 | 不需要(预算够) | `expand_mode` + `expand_page` 全套 | TBD |
| 引用回复 | `/messages/{id}/reply` 端点 | `reply_parameters.message_id` + `quote` 截 100 字 | 不适用 |
| toast / 错误反馈 | 发一条小卡 | `answer_callback_query.text` 原生 toast | TBD |

**共享的**:Model + ViewModel + 大部分状态机 / 事件处理 / 按钮抽象。
**分开的**:渲染函数 + adapter callback 路由 + 平台特有的 reply / toast。

---

## 5. 一个新 channel 怎么接

照抄 `TelegramAdapter` 的骨架:

```python
class MyChannelAdapter(Adapter):
    adapter_id = "mychannel"

    def __init__(self, *, config, bindings, api_client=None, pairing=None):
        self._inbound = asyncio.Queue[InboundMessage]()
        self._buffers: dict[str, _SessionBuffer] = {}  # session_id → buf
        ...

    # 入站
    async def _on_msg(self, raw):
        text, chat_id, msg_id, user_id = parse_raw(raw)
        if not await self._gate(user_id, chat_id, text): return
        session_id = self._bindings.allocate(chat_id, prefix="mc")
        buf = self._get_or_create_buffer(session_id)
        buf.chat_id = chat_id
        buf.pending_reply_to = msg_id  # 引用框
        await self._inbound.put(InboundMessage(
            adapter_id=self.adapter_id, session_id=session_id,
            text=text, user_id=user_id,
            metadata={"chat_id": chat_id, "message_id": msg_id},
        ))

    # 出站 — Card 通过 bind_transport 注入到 deliver
    async def _deliver(self, buf, card):
        rendered = my_render(card.state)
        if card.message_id is None:
            mid = await self._api.send(... reply_to=buf.pending_reply_to ...)
            card.message_id = mid
            buf.pending_reply_to = None
        else:
            await self._api.patch(card.message_id, rendered)

    # AgentEvent → Card.dispatch
    async def render(self, event: AgentEvent) -> RenderedMessage:
        buf = self._get_or_create_buffer(event.session_id)
        card_id = self._card_id_for(event)
        card = self._ensure_card(buf, card_id)
        await card.dispatch(event)
        return RenderedMessage(...)

    # ProgressEvent → Card.dispatch
    def note_progress(self, payload: dict) -> None:
        ...同上,从 payload 路由到 card.dispatch(ProgressEvent(...))
```

按钮回调 / 命令路由参照 `_handle_callback_query`(TG)或 `_on_card_action`(飞书)。

---

## 6. 命令 / 按钮约定

| 按钮 | callback 形态 | 处理位置 |
|---|---|---|
| 取消回答 | `send=/cancel <run_id>`(走 inbound + command_router) | `_cmd_cancel` |
| 后台回答 | `send=None`(adapter 本地路由到 detach) | adapter `__detach__:` |
| `+N 待加载工具` / `显示剩余 N 工具` | `action=load_tools, action_arg=card_id` | adapter `__load_tools__:` |
| 展开 思考 / 工具 / 回答 | `action=expand, action_arg=card_id:section` | adapter `__expand__:` |
| 翻页 / 返回 | `action=expand_page` / `expand_back` | adapter `__expand_page__:` / `__expand_back__:` |
| `/cancel` 无参 / `/attach` 无参 | 列出后台 run 菜单卡 → 每项 `send=/cmd <run_id>` | `_cmd_cancel` / `_cmd_attach` |
| /help 子命令 | `send=/cmd`(每项一个按钮) | command_router |

按钮抽象在 `unified_card.Button(text, send, action, action_arg)`。每平台 renderer 自己把它翻译成 inline button。

---

## 7. 引用 / 回复(quote box)

用户的消息 id 入站时已存在 `buf.pending_reply_to`(+ TG 还有 `pending_reply_quote = text[:100]`)。`_deliver` 首次 send 时消费一次。

- **飞书**: `send_card(reply_message_id=...)` → 走 `/messages/{id}/reply` 端点
- **TG**: `send_message(reply_to_message_id=..., reply_quote=...)` → 走 `reply_parameters` + `quote` 字段(原话前 100 字)
- 后续 patch / edit 不再带 reply

引用框不占消息 body 长度。

---

## 8. 取消语义(快取消)

- `gateway.cancel(session_id, run_id)` 触发 `abort_signal.trigger()`
- harness 在 `client.stream()` 循环每个 event 前 poll `abort.aborted` → 立刻 break
- read_only 工具:`tool.cancel(call_id)` → 工具立刻 raise CancelledError
- write 工具:`tool.cancel()` → 工具自己 SIGTERM + 2s grace + SIGKILL
- harness 收尾 → emit `cancelled` HarnessEvent → footer 🔴

后台按钮:`Card.freeze()` 用 `coalescer.finalize(wait_inflight=False)` 跳过等待,瞬时冻结。

---

## 9. 失败模式 / 防坑清单

- TG MarkdownV2:任何用户文本必须经 `_esc()`(转 reserved 字符);模型 markdown body 走 `_md()`(telegramify-markdown);截断时 ` ``` ` 必须配对(否则 "Can't find end of Pre")
- 飞书 collapsible_panel **不接受** `background_color` / `border` / `padding` 这些字段,加了就 200800 universal card fail
- 飞书 markdown 把 `![alt](url)` 当 image_key,会 200570 invalid image keys。送进去前 sanitize
- TG inline keyboard 一行字符长度上限 ~64,callback_data 必须截
- TG long-poll(getUpdates timeout=25)在网络抖时会 SSL/409/RemoteProtocolError,**这是 TG / 出口问题,不是 xana bug**(对照 yuxu 同时段日志可验证)
- 卡片 in-memory keyed by run_id;serve 重启 / `/clear` 后旧 card_id 失效 → 用户点旧按钮:TG 弹 toast,飞书发小卡提示"已过期"

---

## 10. 最小接入示例

```python
# 你的 agent
class MyAgent(Agent):
    async def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind="draft", session_id=request.session_id,
                         harness_event=HarnessEvent(kind="turn_started", ...))
        async for chunk in your_llm.stream(request.text):
            yield AgentEvent(kind="draft", session_id=request.session_id,
                             harness_event=HarnessEvent(
                                 kind="model_delta", payload={"text": chunk}, ...))
        yield AgentEvent(kind="final", session_id=request.session_id,
                         payload={"text": full_text}, ...)
```

`AgentEvent.harness_event` 不是必须的,但**强烈建议有**:不带 harness_event 的 AgentEvent 走 `_apply_agent_only`(命令结果路径),会被当成菜单卡处理。
