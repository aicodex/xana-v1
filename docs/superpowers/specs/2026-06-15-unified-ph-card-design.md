# 统一 PH 卡片模型 — Spec

> 2026-06-15
> 上游决议:用户 2026-06-15 brainstorming 锁定。
> 替代:`feishu_card.build_fg_streaming_card` / `build_bg_placeholder_card` / 老 compact 卡 builder / telegram_html 的等价路径。**同 PR 删除旧代码**。
> 范围:**feishu + telegram 同步落地**。CLI(Textual REPL)未来 UI 整包一起改,本 spec 不强制。

---

## 1. 目标

让 chat / compact / detach / menu / `/help` / `/attach` 等所有"adapter 渲染"场景共享**同一张卡片模板**,按需出现 section。前台 / 后台 / 终态共用模板,只是 section 内容和控件差异。

**核心不变量**:
1. **1 PH = 1 卡**:卡片对应 active_context 里一个 placeholder 位置;PH 没出现的命令响应(纯菜单)对应一张菜单专属卡。
2. **只 edit 不 recreate**:卡片创建后永远 patch 同一 message_id,保证位置不动。
3. **后台不刷新**:detach 瞬间一次 patch(进入冻结态),`ph_replaced` 瞬间一次 patch(终态),期间零更新事件。
4. **detach 不改高度**:detach 瞬间确定骨架,终态切换不引起 reflow。

---

## 2. 卡片骨架(自顶向下)

```
┌──────────────────────────────────────┐
│ [顶部按钮区] (可选)                   │
│                                      │
│ [compact 段] (可选)                   │
│   主标题: 正在压缩上下文 → 上下文压缩完成 │
│   进度条 ▰▰▰▱▱▱▱▱▱▱  (10 格,无 %)    │
│   下拉(占位): 压缩中 → 已压缩 N 条     │
│                                      │
│ [回答段] (可选)                       │
│   thinking 块(下拉)                  │
│   tools 块 × ≤ N(下拉)               │
│   tools 提示块 + 按钮                 │
│   回答块                             │
│                                      │
│ [菜单段] (可选)                       │
│   菜单回复内容                        │
│                                      │
│ [底部按钮区] (可选)                   │
│                                      │
│ [脚标提示区]                          │
└──────────────────────────────────────┘
```

各 section 按需出现,顺序固定。下拉一律占位渲染(空时也存在),避免视觉抖动。

平台限制:**Telegram inline_keyboard 总在底部**,顶部按钮在 TG 上会被强制下沉到 inline_keyboard;飞书按 spec 位置渲染。

---

## 3. Section 出现规则

| Section | 出现条件 |
|---|---|
| 顶部按钮区 | 有顶部按钮内容(见 §6) |
| compact 段 | 该 PH 是"earliest unfinished writeback"且正在 / 已 compact;手动 `/compact` 无 in-flight 时也在 |
| 回答段 | 该 PH 关联一个 chat run(任意状态) |
| 菜单段 | 纯命令场景(`/help` 等),无 compact + 无回答 |
| 底部按钮区 | 有底部按钮内容(`/attach` 候选列表、菜单按钮) |
| 脚标提示区 | 始终出现(三态见 §7) |

> **关键**:auto-compact 触发时,compact 段挂在"未完成回写最前面 PH"的卡上(可能是后台 run 的卡或前台被打断转后台的卡)。手动 `/compact` 走同一规则:若有 in-flight PH,挂前面那个;否则自己创建一张菜单+compact 卡。

---

## 4. 状态机

### 4.1 Compact 段状态

| 状态 | 主标题 | 进度条 | 下拉 title |
|---|---|---|---|
| input | 正在压缩上下文 | ▰▱▱▱▱▱▱▱▱▱(线性) | 压缩中 |
| streaming | 正在压缩上下文 | ▰▰▰▰▰▰▰▱▱▱ → ▰▰▰▰▰▰▰▰▰▰ | 压缩中 |
| completed | 上下文压缩完成 | ▰▰▰▰▰▰▰▰▰▰ | 已压缩 N 条消息 |
| cancelled | 压缩已取消 | (隐藏) | 压缩已取消 |

进度条规则:
- **0 → 7 格**:输入累积阶段(prefill),时间估计 20s 总长 → 2.86s / cell 线性涨。**首个 stream delta 一到,直接跳到 7**。
- **7 → 10 格**:输出阶段,`SUMMARY_SECTIONS` 7 个 section,每完成一个推进 0.43 cell(7 + 0.43*N,N=0..7)。
- 终态(completed):10 格满;cancelled:隐藏。

> 后续:`generate_summary` 加日志记 prefill 时间(请求发出 → 首 delta)和 output 时间(首 delta → done)。所有 llm 调用都加,便于校准估计。

### 4.2 回答段状态

| 状态 | thinking | tools 块 | 回答块 |
|---|---|---|---|
| input/streaming(fg) | "正在思考" → "思考过程"(展开/折叠用户控) | 滚动累积,自动展开最新 | 展开,正在回答 → 回答 |
| detach 瞬间 | 折叠 + title "思考过程" | 全部折叠 | **折叠 + 占位"回答中…"**(避免高度抖动) |
| 终态(ph_replaced) | 折叠 | 折叠 | 折叠,内容替换为最终"回答" |

> 飞书:detach 状态用 `collapsible_panel` 折叠 + 内部 md 显示状态字符串("回答中…")。终态:同一 panel 内容更新为最终文本。
> Telegram:detach 用 `<blockquote expandable>` 折叠("回答中…")。终态:折叠内容替换为最终回答。
>
> **两平台共享**:detach 那一瞬间确定 panel 数量 + 折叠状态,后续 patch 只改 panel 内 text,不增删 panel。

### 4.3 Tools 块详情

- 默认显示前 3 个 tool panel。
- 若总数 > 3,显示一个 **tools 提示块 + 按钮**,文案 `N+ 待加载工具`,N = 已累积总数 - 已显示数。点击 → load 剩余全部(从此该 tools 提示块消失或显示 `已全部加载`)。
- 若总数 ≤ 3 但回答段在 streaming:tools 提示块显示 `正在组织回答`(占位)。
- 一旦累积超 3,提示块切到 `N+ 待加载工具` + load 按钮。

---

## 5. 顶部按钮规则

| 卡状态 | 顶部按钮 |
|---|---|
| 前台 + 仅回答段 + running | ⏹ 停止  ➡ 后台 |
| 前台 + 回答段 + compact 段(都 running) | ⏹ 停止  ➡ 后台 (**compact 不可取消**,因为取消了 compact 立马会被同 run 再次唤醒) |
| 前台 + 仅 compact 段 + running(手动 /compact,无 in-flight) | 取消 |
| 前台 + 回答段 + compact 段已完成 + 回答 running | ⏹ 停止  ➡ 后台 |
| 后台(任何 section running)| —(detach 瞬间清空) |
| 终态(全 section completed/cancelled) | — |

**例外**:`/attach` 命令触发时,候选列表渲染为底部按钮(顶部不出现 attach 按钮)。流式刷新中的卡片不允许出现 attach 按钮(只能 停止/后台)。

---

## 6. 底部按钮规则

| 场景 | 底部按钮 |
|---|---|
| `/attach` 候选列表 | 每个候选 run 一个按钮,点击 → `/attach <run_id>` |
| 菜单 + 子选项(`/model` / `/effort` / `/streaming` / `/thinking` / `/async`) | 选项按钮区 |
| 其它 | — |

---

## 7. 脚标三态

| 状态 | 脚标内容 |
|---|---|
| 前台 running | `<model> · 上下文 N/M · 用时 1h2m1s` |
| 后台 frozen(detach 后到 ph_replaced 之前) | `<model> · 🟡 后台运行中 · 用时 1h2m1s` |
| 终态 | `<model> · 上下文 N/M · 用时 1h2m1s` |

**时间格式**:`Xh Ym Zs`。从最大非零单位起显示:
- `5s` / `1m 5s` / `1h 2m 1s` / `2h 0m 30s`(中间 0 保留以避免歧义,但**前导 0 单位不显示**)
- 阈值:超过 1 单位才出现该单位(`5s` 不显示 `0h 0m 5s`)

---

## 8. 事件契约(gateway → adapter)

`note_progress` 接收的事件 kind:

| kind | 时机 | 卡变化 |
|---|---|---|
| `run_started` | gateway 起新 run | 创建新卡;骨架按 PH 类型(回答段+顶部按钮) |
| `compact_started` | gateway 起 compact run | 在对应 PH 卡前插入 compact 段;若无 PH 则新建卡 |
| `model_delta` | stream text 增量 | patch 对应段对应块(thinking / answer / compact 下拉) |
| `tool_started` / `tool_finished` | 工具事件 | 累积 tools 块;>3 时切到提示块 |
| `compact_progress` | compact 阶段事件 | 更新进度条 + 下拉 title |
| `run_detached` | 用户点 后台 / 发新消息 / `/compact` 自动 detach 等 | detach 瞬间一次 patch(折叠所有 panel、占位"回答中..."、清按钮、脚标改"🟡 后台运行中") |
| `ph_replaced` | run 真正完成回写 | 一次性 patch 终态(回答块替换为最终文本、脚标改回前台格式) |
| `cancelled` | run 被取消 | 同 ph_replaced 但状态显示为"已取消" + warning(若有) |
| `final` / `error` | run 终结(无 PH 替换) | 同 ph_replaced 处理终态 |
| `menu_response` | 命令响应(`/help` 等) | 创建 / patch 菜单段卡 |

---

## 9. PH 顺序不变量

- 卡片创建后,**绝不重建**,只 patch 同一 message_id。
- 新 run / 新 compact → append 新卡在尾部(自然时序在后)。
- 用户消息插入位置不可控,但 PH 卡总是出现在触发它的消息**之后**(因为 PH 是消息触发的产物)。
- 后台 detach 不移动卡片位置。

---

## 10. 渲染 API

### 10.1 数据模型(adapter 中)

```python
@dataclass
class UnifiedCardState:
    card_id: str                       # 内部唯一,1:1 对应 PH 或菜单卡
    message_id: str | None             # 平台 message_id;首次发送后填
    ph_run_id: str | None              # 关联的 PH run_id;菜单卡为 None

    top_buttons: list[ButtonRow]       # §5
    compact: CompactSection | None     # §4.1
    answer: AnswerSection | None       # §4.2
    menu: MenuSection | None           # 命令场景
    bottom_buttons: list[ButtonRow]    # §6
    footer: FooterText                 # §7

    detached: bool                     # 后台冻结态
    terminal: bool                     # 终态
```

### 10.2 渲染入口

```python
def render_card(state: UnifiedCardState) -> dict:    # feishu 返回飞书卡 schema
def render_card(state: UnifiedCardState) -> str:     # telegram 返回 HTML
```

每个 adapter 维护一个 `dict[card_id, UnifiedCardState]`,事件来 patch state,然后 render 出新卡 / HTML 推送。

---

## 11. 清理(同 PR 删)

落新模板的同 PR 删:
- `src/xana/agent_adapter/channels/feishu_card.py` 删 `build_fg_streaming_card` / `build_bg_placeholder_card` / 老 compact 卡 builder(`build_compact_card` 等)
- `src/xana/agent_adapter/channels/telegram_html.py` 等价路径
- `src/xana/agent_adapter/channels/feishu.py` / `telegram.py` 改用 `render_card`
- 对应测试:删旧 card builder 的单元测试,加新 UnifiedCard 的单元测试

---

## 12. 不在本 spec 范围

- CLI(Textual REPL)的 UI:走独立"CLI 整包"(参考 memory `project_cli_new_framework_pending`)。Textual 用同一 `UnifiedCardState` 模型但渲染路径独立。
- `/compact` 内部算法 / fork_point / 子分支 compact:见 `2026-06-14-p5-p6-decisions.md` §P6 + `2026-06-13-async-run-ROADMAP.md` §Phase 6。
- LLM 调用计时(prefill / output 日志):本 spec 只提需求,实施在另一处。

---

## 13. 接口改动审批门

按 `feedback_interfaces_locked`,本 spec 落地期触碰:

- **新增** `src/xana/agent_adapter/channels/unified_card.py`(数据模型 + render 入口),不算改既有 contracts/interfaces。
- 可能改 `agent_adapter/interfaces.py` 的 `Adapter` 接口:若需要新增 `render_card_state(state)` 方法替换现有 `render(event)`。**实施期与用户确认**。
- `note_progress` 事件 kind 列表扩展(§8):菜单响应 `menu_response`、`compact_started`,可能不在现有枚举里 — **实施期审批**。
