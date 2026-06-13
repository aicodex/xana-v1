# 统一 PH 驱动渲染 + Textual REPL 设计稿

> 范围：Async Run Phase 6+ 的"渲染模型重写" + xanac REPL 改 Textual + oneshot/interactive 管线合并。
>
> 依赖：P1–P5 已落（main.jsonl、write queue + consumer、Placeholder 契约、双模 Harness、Cancel/Resume）。
> 前置阅读：`docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`、`best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`。

---

## 1. 核心转变

P4 落地的渲染模型："事件驱动 per-run `_TurnState` + detach 后弹纯回答卡"。本期废除"纯回答卡"概念，统一为 **PH 驱动渲染**：

- main.jsonl / active context 里的 Placeholder = 真相源；adapter 端的卡片状态由 PH 状态机驱动。
- bg run **不丢事件**——和 fg run 在 adapter 内部行为完全相同；区别仅在「是否绑定到当前显示焦点」。
- PH replacement（consumer 单写者完成）= adapter 把对应卡刷成「完整卡」（含 thinking/tool 折叠、answer、footer），不是另起一张纯回答卡。

---

## 2. Run / 卡片状态机

```
                  ┌─── (final) ───────────────────► finalized_full
                  │
streaming_fg ─────┤
                  │
                  └─── (new user msg / switch_foreground out) ──► streaming_bg
                                                                    │
                                            ┌── (user switch back) ─┤
                                            │                       │
                                            ▼                       │
                                       streaming_fg ◄───────────────┘
                                            │
                                            └── (final) ─────────► finalized_full
                                                                    
streaming_*  ── (cancel/error) ──► finalized_failed
```

卡片视觉态：

| Run 状态 | 卡片视觉 |
|---|---|
| `streaming_fg` | 实时刷新（thinking/tool/answer 增量） |
| `streaming_bg` | "运行中" PH 占位卡：顶部 chip + 折叠"已产出的部分"，**无 stop button**，**无任何编辑动作发到渠道** |
| `finalized_full` | 完整卡：thinking 折叠 + tool 折叠 + answer + footer_meta + buttons |
| `finalized_failed` | 失败卡：原因 + partial 折叠 |

---

## 3. Adapter 内部模型

### 3.1 per-session 状态

```
RunRenderState:
  run_id: str
  turns: OrderedDict[turn_id, TurnDraft]     # 持续累积，bg 期间也写
  card_anchor: ChannelMessageId | None       # 飞书 message_id / TG message_id / Textual widget id
  is_foreground: bool
  is_frozen: bool                            # streaming_bg=True，streaming_fg=False
  label: str                                 # 首条 user_input 前几字，给 tab 用

SessionRenderState:
  runs: OrderedDict[run_id, RunRenderState]  # 最近 N（4）
  foreground_run_id: str | None
```

### 3.2 事件路由

`gateway.submit` 内已按 run_id 标记 ProgressEvent；adapter 的 `_make_send` 改为：

```
on event(run_id, kind):
  state = runs[run_id]
  累积 turns[turn_id]
  if state.is_frozen:
    return                       # 不发任何渠道动作；状态继续累积
  else:
    edit/patch card_anchor
```

### 3.3 切换前台（switch_foreground）

```
on foreground_changed(target_run_id):
  prev = foreground_run_id
  if prev: runs[prev].is_frozen = True;  freeze_card(prev)
  foreground_run_id = target_run_id
  runs[target_run_id].is_frozen = False
  full_rerender(target_run_id)             # 全量拉卡：用 turns 累积重渲一张新卡
```

**全量重渲** = 新开一张卡（新 message_id），按 `turns` OrderedDict 顺序渲染所有 turn。原 fg run 的卡保留为 frozen 状态。

理由：渠道侧不能"复用旧 message 重新插队到最新位置"，新 message 才在视觉上是"当前"。

---

## 4. PH 生命周期与渠道映射

### 4.1 生命周期事件

consumer 在 main.jsonl 写入 PH/替换时，gateway 发出对应 ProgressEvent：

| 事件 | payload | adapter 行为 |
|---|---|---|
| `run_started` | run_id, label | 新建 RunRenderState；若现有 fg → 标 detached |
| `run_detached` | run_id | 标 frozen；冻结卡（停止 patch） |
| `foreground_changed` | target_run_id | 切换 fg + 全量重渲（见 3.3） |
| `ph_replaced` | run_id, kind=full\|failed, draft | 替换 frozen 卡为 finalized_full/failed；若 PH 当时是 fg（说明 run 是 fg 完成）→ 同卡 finalize |
| `run_completed_bg` | run_id | 不抢焦点：在 frozen 卡位置就地刷成 finalized_full（已被 ph_replaced 覆盖时 no-op） |
| `compact_started` | compact_run_id, fork_point | 在 fork_point 位置插入 compact 卡（独立 message） |
| `compact_finalized` | compact_run_id, summary | compact 卡定稿（summary + 折叠原始范围）；失败→标 ✖ |

### 4.2 飞书具体动作

| 状态变更 | 飞书动作 |
|---|---|
| streaming_fg → streaming_bg | 把当前 message 编辑为「PH 冻结卡」：顶部 chip "🟡 运行中"，folded panel "已产出"，**移除 buttons** |
| 切回 fg | 新发一条 message，渲染完整 turn 历史；旧冻结卡保留 |
| PH 替换（完整） | 编辑「冻结卡 message」为完整卡（不发新消息） |
| compact 卡 | 独立 message，独立 run_id |

### 4.3 Telegram 具体动作

废除"每个事件单独 message"，全程对同一 message_id 走 editMessageText：

| 状态变更 | TG 动作 |
|---|---|
| run 起 | 新 sendMessage 拿 message_id，写入 RunRenderState.card_anchor |
| streaming 期间 | 持续 editMessageText 同一 message |
| 冻结 | editMessageText 为 PH 文本（"🟡 运行中"+ partial 引用），inline keyboard 移除 |
| 切回 | sendMessage 新条 + 渲染全量 turns（旧条不动） |
| PH 替换 | editMessageText 旧冻结 message → 完整卡格式 |
| tabs | inline keyboard 加 "切回 [label]" 按钮，callback_data=run_id |

### 4.4 CLI/Textual

- StdoutRenderer：oneshot 单 run，不可能 detach，状态机退化为 streaming_fg → finalized_full 直线。
- TextualRenderer：每个 run 一个 RichLog/Markdown widget 容器；切换 fg = 切换可见容器；tab bar = bg run 列表。

---

## 5. Compact 渲染

### 5.1 触发与代码统一

手动 `/compact` 和自动 fork_before compact 走同一份代码：
- handler 调 `start_compact_run(session, fork_point)`：在 fork_point 位置 append `Placeholder(run_id=compact_*, kind="compact")`，启动 compact run。
- 渲染：adapter 收 `compact_started` → 渠道侧新建 compact 卡（独立 run_id）。

### 5.2 与 user run 并发

| 场景 | 行为 |
|---|---|
| compact 期间用户发新消息 | compact_run detach（同普通 run）→ frozen 冻结卡（无 stop button：compact 本就不可取消）；新 run fg |
| compact 失败/cancel | finalized_failed 标 ✖，fork_before 不前移；下次 compact 重新 fork |
| 完成 | consumer 串行 replace_range → adapter 收 compact_finalized → compact 卡定稿（summary + 折叠原范围）|

### 5.3 fork_point 之后立刻继续渲染

compact 卡只占 fork_point 位置；fork_point 之后的新 run（包括引发 compact 的 user prompt）立即在 compact 卡下方开新卡渲染，不等 compact 完成。视觉上：

```
... (历史卡，将被 compact 覆盖) ...
[compact 卡 ⏳ 运行中]      ← fork_point
[user prompt 卡] [thinking/tool/answer 流式]   ← 立刻继续
```

compact 完成后：

```
... (历史卡视觉不变，渠道不能撤回旧 message) ...
[compact 卡 ✅ 摘要 N 轮]
[user prompt 卡 完整]
```

注：渠道侧旧消息不撤回；compact 卡作为 "↑ 已压缩 ↑" 视觉分隔。

---

## 6. 命令 detach 策略（最终版）

| 命令类 | flag | inflight 追踪 | detach 当前流 | 自身可被 detach |
|---|---|---|---|---|
| `/compact` | triggers_turn=True, cancellable=False | 是 | 是 | 是 |
| 状态命令（/help, /clear, /model, /thinking, /streaming, /effort, /async, /search_provider, /project, /resume 列表） | 默认 | 否 | 否（当前流不受影响） | N/A |

实现：gateway `_submit` 看 CommandResult.payload.triggers_turn——False 时不进 `_inflight`，不发 `run_detached`，命令卡直接渲染独立 message。

---

## 7. xanac → Textual + oneshot 复用

### 7.1 入口

```
xanac                    # 进 Textual TUI（interactive）
xanac "msg"              # one-shot：单次 inbound → 流式 stdout → 退出
xanac < input.txt        # 同 one-shot
```

### 7.2 复用边界

共用：CliAdapter（gateway 入站统一）、ChatAgent、harness、boot 组装。

分叉：CliAdapter 渲染层抽出 `CliRenderer` ABC：

```python
class CliRenderer(ABC):
    async def on_run_started(self, run_id: str, label: str) -> None: ...
    async def on_event(self, run_id: str, event: HarnessEvent) -> None: ...
    async def on_run_detached(self, run_id: str) -> None: ...
    async def on_foreground_changed(self, run_id: str) -> None: ...
    async def on_ph_replaced(self, run_id: str, draft: TurnDraft) -> None: ...
    async def on_run_completed_bg(self, run_id: str, draft: TurnDraft) -> None: ...
    async def on_compact_started(self, run_id: str, fork_point: int) -> None: ...
    async def on_compact_finalized(self, run_id: str, summary: str) -> None: ...
```

- `StdoutRenderer`：oneshot；detach/foreground_changed/tabs 全 no-op（单 run 不会触发）；on_event 流式 print；on_ph_replaced 退化为 print summary。
- `TextualRenderer`：interactive；驱动 Textual App。

### 7.3 Textual App 结构

```
App
├─ Header
├─ Horizontal
│   ├─ RunPane(fg)         ← VerticalScroll, 多个 TurnCard widget
│   └─ TabSidebar          ← bg run 列表; Enter 切换
├─ Input
└─ Footer (status / hints)
```

- Tab key：focus 切到 TabSidebar；上下选；Enter 触发 `gateway.switch_foreground`。
- Input 提交：`gateway.submit(InboundMessage)`。
- TurnCard widget：Markdown + collapsible thinking/tool。
- 全量重渲：切 fg 时 TabSidebar 选中项变 RunPane，旧 RunPane 被推到 TabSidebar 头部（frozen 视觉）。

### 7.4 oneshot 与 interactive 管线合并点

| 层 | 共用 | 备注 |
|---|---|---|
| CliAdapter.receive() | ✓ | oneshot：单次 yield 后 EOF；interactive：Textual Input → asyncio.Queue → yield |
| CliAdapter.note_progress | ✓ | 都分发到 CliRenderer |
| CliAdapter.render(event) | ✓ | 都把事件 forward 到 renderer.on_event |
| gateway / agent / harness | ✓ | 完全相同 |
| Renderer 实现 | ✗ | StdoutRenderer vs TextualRenderer |

CliAdapter 构造时注入 renderer。

---

## 8. 并发推演表（完整覆盖）

| # | 场景 | 流程 |
|---|---|---|
| 1 | U1 → U2 快速 | run1 fg → run_detached → run1 frozen PH 卡；run2 fg。run1 完成 → consumer 替换 PH → frozen 卡定稿完整卡 |
| 2 | U1 → U2 → U3 | run1 bg, run2 bg, run3 fg；tabs 显示 [run1, run2]。完成顺序无关：各自 PH 替换 → 对应 frozen 卡定稿 |
| 3 | Tab 切回 run1 | switch_foreground(run1) → run3 detach（frozen PH）→ run1 全量重渲（新卡，turns 累积重放）→ run1 后续事件继续 patch 新卡 |
| 4 | 切回 run1 时它已完成 | foreground_changed → 全量重渲；后续没事件来；卡保持 finalized_full（不再有 PH 替换，因 ph_replaced 已先到） |
| 5 | `/compact` 期间 U2 | compact_run fg → user 发 U2 → compact_run detach（无 stop button：cancellable=False）→ run2 fg。compact 完成 → consumer replace_range + ph_replaced → compact 卡定稿摘要 |
| 6 | bg run 完成时用户在看别人 | 不抢焦点；run_completed_bg → 对应 frozen 卡定稿完整卡；tab 该项消失 |
| 7 | Cancel bg run | bg 卡无 stop button；用户须先切回 fg 才能 cancel。设计上这是预期：取消是"看着的"动作 |
| 8 | 重启时有 running PH | replay main.jsonl → PH 状态 running → adapter（重启后首次 ProgressEvent）按 PH 状态机展示 frozen 冻结卡。P5 `resume_run` 若 fork 出新 run → adapter 收 run_started + 同 run_id → 从 frozen 转 streaming（事件继续累积）|
| 9 | compact replace_range vs PH 替换并发 | write queue 单 consumer 串行；adapter 按事件顺序收 ph_replaced / compact_finalized |
| 10 | 飞书断网 / TG editMessage 失败 | adapter 局部捕获并 log；frozen 卡可能停留在"运行中"视觉直到下一次刷新成功——本期不做重试（YAGNI） |
| 11 | 自动 compact 在 run1 内部触发 | 该 run 内部首次触发 = fork_before compact：开一个独立 compact_run + PH 在 fork_point。run1 继续。compact 完成 → replace_range。同场景 #5 |
| 12 | 状态命令在 streaming 期间 | 不进 inflight，不 detach；直接发命令卡。当前流不受影响 |
| 13 | 切回后立刻又发新消息 | run1 fg（刚切回）→ U4 → run1 detach 回 bg → run4 fg。状态机正常流转 |
| 14 | 同一 run 收到 final 时它是 bg | event 累积到 turns；frozen 卡视觉不变；等 ph_replaced 才定稿（与 #6 相同） |
| 15 | Renderer 实现层崩溃 | adapter 调用包 try/except + log；不影响 gateway 主循环 |

---

## 9. 接口改动清单（需用户批准）

按 `feedback_interfaces_locked.md`，contracts/interfaces 改动必须显式批准。

**`agent_adapter/contracts.py`**：
- `ProgressEvent` 已有 `kind` channel，复用，无需改契约（新 kind 是字符串值，不是字段）。
- 删除 P4 的 `InflightRun.detached: bool` 改名 → 仍叫 `detached`，语义不变，无需改。

**`agent_adapter/interfaces.py`**：
- 新增 `CliRenderer` ABC（只影响 CLI 侧，新接口；走"先问后加"规范）。

**`memory/contracts.py`**：
- 已有 `Placeholder`（P2 加），本期不改。

**新 ProgressEvent kind 值**（字符串，非契约改动）：
- `ph_replaced`, `compact_started`, `compact_finalized`

**`agent_adapter/contracts.py` 新增 CommandResult flag**：
- `triggers_turn: bool = False`（P5 已规划）
- `cancellable: bool = False`（仅 `/compact` 例外置 True 的反义；本期 compact 设 False，体现"不可 cancel"）

**待用户确认**：
1. 同意新增 `CliRenderer` ABC 到 `agent_adapter/interfaces.py`。
2. 同意 `CommandResult.payload` 引入 `triggers_turn` + `cancellable` 字段（dict[str, Any] 现状下放 payload 即可；若改 dataclass 字段需额外批准）。
3. 同意新增 ProgressEvent kind 值：`ph_replaced` / `compact_started` / `compact_finalized`。

---

## 10. 废弃 / 改造清单

| 既有代码 | 处置 |
|---|---|
| `feishu_card.build_pure_answer_card` | 删 |
| `telegram_html.format_pure_answer_html` | 删 |
| `_SessionBuffer.started_as_detached` (飞书/TG) | 删（PH 状态从 RunRenderState.is_frozen 来） |
| `CliAdapter` 现有 print 流 | 重构进 StdoutRenderer |
| `xanac` 现有 prompt_toolkit PromptSession | 替换为 Textual App |
| `gateway._inflight` 模型 | 保留 + 扩展（加 PH 事件触发） |
| `gateway.switch_foreground` | 保留 + 改为发 foreground_changed 事件给 adapter |

---

## 11. 不在本期范围

- 跨渠道同步（同 session 在 TG + 飞书 同时打开）。
- 渠道 message 撤回（compact 不删历史卡）。
- 卡片重试（断网失败不自动重发）。
- `/resume` 列表的渲染（P5）。
- 后台 run 进度通知（弹窗/铃声）。

---

## 12. 任务分组（供后续 writing-plans 用）

```
Group A — Contracts/Interfaces 批准 + 新增（先做，阻塞所有）
  - CliRenderer ABC 加入 interfaces
  - ProgressEvent 新 kind 值约定（无契约改动）
  - CommandResult.payload flag 约定
Group B — Gateway PH 事件路由
  - consumer 完成 PH 替换 → emit ph_replaced
  - compact start/finalize → emit compact_started/compact_finalized
  - 状态命令绕过 _inflight（不 detach）
Group C — 飞书 Adapter PH 状态机
  - RunRenderState + frozen 卡渲染
  - 全量重渲实现
  - inline button "切回" (使用现有 button infra)
Group D — TG Adapter 单卡合并 + PH 状态机
  - card_anchor 改 message_id 全程 editMessageText
  - frozen 卡格式
  - inline keyboard "切回"
Group E — CliAdapter Renderer 抽出
  - CliRenderer 接口落地
  - StdoutRenderer 实现（覆盖 oneshot）
Group F — Textual REPL
  - Textual App + widget tree
  - TabSidebar + Input
  - TextualRenderer 实现
  - xanac 入口分流
Group G — Compact 渲染对接
  - compact_run 在 fork_point 插入 PH 的 trigger 点
  - adapter 收 compact_* 事件渲染
Group H — 清理废弃代码
  - build_pure_answer_card / format_pure_answer_html 等
Group I — 回归
  - 并发推演表 15 个场景的集成测试
Group J — 沉淀文档（用户测完通过后）
  - 改 ASYNC_RUN_DESIGN.md（best_agent/ds_review）
  - 新增 xana docs/design/bg-streaming.md + compact-rendering.md
  - ROADMAP P6 🟡 标 ✅
  - 一次 commit
```

---

## 13. 验收后沉淀（用户测过且无问题后做）

属于本期最末一步任务，不是文档自娱：

1. **`best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`**：
   - §卡片渲染模型 整段重写：废"占位符不渲染 / 纯回答卡"，落入本 spec §2 + §4 的 PH 驱动状态机。
   - §Adapter 行为 表里 CLI 行加 "Textual（interactive）/ stdout（oneshot）" 拆分。
   - §Compact §手动 /compact 补充 §5.3 的"fork_point 之后立刻继续渲染" 语义。
   - 顶部注释加 "2026-06-14 渲染模型修订" 一行索引指回 xana 本 spec 路径。

2. **xana `docs/` 下补两份新的详细设计**（README_ai 不放代码细节，详设单独成文）：
   - `docs/design/bg-streaming.md`：bg run = fg run 同行为、PH 状态机、全量重渲、tab/switch_foreground、命令 detach 策略。
   - `docs/design/compact-rendering.md`：手动/自动同代码、fork_point 插 PH、立刻继续渲染、replace_range 与 PH 替换 consumer 串行。
   - 两份都引本 spec 为权威源，简述 + 代码定位（contracts/interfaces/实现文件路径）。

3. **`docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`**：
   - 把 P6 🟡 段标记为"✅ 已落地（见 specs/2026-06-14-...）"，不再"待讨论"。

4. **提交**：上述三处一次 commit，message：`docs: sink PH-driven rendering design back to ASYNC_RUN_DESIGN + xana detailed docs`。

## 14. 风险

- Textual 学习曲线：xanac 现有 prompt_toolkit 体验需在 Textual 中等价复刻（多行输入、历史、补全）。Textual 自带 Input/TextArea，elia 项目可参考。
- 飞书"切回"button 的渠道限制：飞书卡片 button 数量上限需查。极端多 bg run 时折叠成"更多…"。
- TG "切回" inline button + 历史 message 编辑权限（bot 是否能 edit 自己很久前的 message——TG 限 48h；超时的 frozen 卡 PH 替换走"发新 message"降级路径）。
