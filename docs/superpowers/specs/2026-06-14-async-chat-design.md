# Async Chat —— PH-First 主上下文 + 双卡渲染设计

> 本期渲染重写 + 数据模型补完。承接 `ASYNC_RUN_DESIGN.md`(/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md)
> 与 `2026-06-14-ph-driven-rendering-design.md`,合并修正前一稿里被搁置的 PH 隔离 +
> 切换 UI 误设计。
>
> 验收后此文档作为 `best_agent/ds_review/ROADMAP/ASYNC_CHAT_DESIGN.md` 长期沉淀。

---

## 0. 问题诊断（为什么前一稿不对）

跑通真实多 run 后用户报告:
1. **模型可以"对比另一个 run"** —— PH 隔离没真做,run2 fork 时实际看到了 run1 的实时输出。
2. **界面跳得厉害** —— 切回触发新建卡 + 渠道侧旧 message 没法插队回最新位置,视觉抖动严重。
3. **切换语义复杂** —— "切回时该往哪写"、"重复切回多张卡"等边界值堆积。

诊断:
- 数据层:`HarnessContext.async_mode=True` 时没在 main 里真插 PH;run2 forked_context 实际是
  "main snapshot 加锁拍到的瞬时态",而 main 仍然被 run1 实时追加。
- 渲染层:把 run 切回前台当成"刷一张新卡"的视觉操作,没把"用户只持有一个流"这个不变量
  落到接口里。

本期同时修复两层。

---

## 1. 数据模型

### 1.1 主上下文 = 单一规范源

```
main = active_context_store + main.jsonl(File 实现已落)
```

所有 run 都对 main。**不再存在「在 main 上跑」和「在 fork 上跑」两条路径**:每个 run 都是
fork,差异仅在 consumer 怎么消费。

### 1.2 Placeholder 契约

`memory/contracts.py` 里已有 `Placeholder` 数据类(P2 加,本期不改字段):

```python
@dataclass(frozen=True)
class Placeholder:
    run_id: str
    label: str         # 用户 prompt 前 40 字
    status: str        # "running" | "completed_pending" | "cancelled" | "failed"
    jsonl_path: str    # 相对路径,如 "runs/r3.jsonl"
```

`ActiveContext.messages: list[MemoryMessage | Placeholder]` —— PH 与普通消息平级存在。

### 1.3 Run 提交流程(sync/async 共用)

```
submit(user_msg):
  with main._lock:
    snapshot = main.snapshot()           # 拍下当前所有 messages(含已有 PH)
    main.append(user_msg)                # 用户消息立刻可见
    ph = Placeholder(run_id, label, "running", "runs/{run_id}.jsonl")
    main.append(ph)                      # PH 占位,等待回写
    fork_ctx = snapshot + [user_msg]     # run 的工作上下文(不含自己的 PH)
  enqueue(run_id, fork_ctx)              # 给 consumer
```

**关键不变量:**
- run 的 fork_ctx 是 snapshot + user_msg,**不含**自己的 PH,**不含**别人后续追加的 PH/消息
  (snapshot 是当时态)
- run 跑的过程中如果 main 又被追加(其他 PH/其他完成的回写),不影响 run 的视图

### 1.4 别人的 PH 在 LLM 视角的呈现

snapshot 里可能包含别的 still-running PH。这些 PH 在送给 LLM 前展开为合成对话对:

```
原 main 中: [..., user_msg_A, Placeholder(r_A, "review xana"), user_msg_B, ...]

LLM 视角(snapshot 展开后):
  ...
  user: review xana
  assistant: [此问题正在后台处理中,请基于其他可用上下文继续。]
  user: <user_msg_B 文本>
  ...
```

实现在 `harness_sdk` 的 prompt 装配层,把 `Placeholder` 节点转成合成 assistant
message。run 自己看不到自己的 PH(snapshot 是 PH 追加前的)。

### 1.5 Sync vs Async 唯一差异 = consumer

```
async consumer (默认):
  for run_id, fork_ctx in queue:
    asyncio.create_task(_invoke(run_id, fork_ctx))   # 不阻塞,允许并发

sync consumer:
  for run_id, fork_ctx in queue:
    await _invoke(run_id, fork_ctx)                  # 阻塞,串行
```

`_invoke` 共用:都走 `DefaultHarness._invoke`,都写 `runs/{run_id}.jsonl`,完成时入写队列
让独立的 **写队列消费者** 做 PH 替换(见 §1.6;run consumer 和 write_queue consumer
是两个独立 task,write_queue consumer 始终并发)。**sync 模式视觉上只会有一个 fg 在跑
(队列里其他 run 还没开跑就没卡)。**

### 1.6 Run 完成 → 写队列单消费者

`runs/{run_id}.jsonl` 最后一行写 `{"kind":"run_completed", "run_id":...}` 后,把 run_id 入写队列。

```
write_queue 唯一消费者(boot 时启动):
  loop:
    item = await write_queue.get()
    if item.kind == "replace":
      messages = read_run_jsonl_except_completion_marker(item.jsonl_path)
      with main._lock:
        idx = main.find_placeholder(item.run_id)
        main.messages[idx:idx+1] = messages
        main_history.append_replace(item.run_id, messages)
      os.remove(item.jsonl_path)
      emit ph_replaced(item.run_id, mode="full")
    elif item.kind == "replace_range":
      # compact: 见 §4
      ...
```

**写队列必须 sync/async 都用** —— compact 也走它(`replace_range`),保证不与 PH 替换并发。

### 1.7 锁模型

| 操作 | 锁 | 理由 |
|---|---|---|
| `main.append(user_msg)`、`main.append(PH)` | session._lock | 写 main |
| `main.snapshot()` | session._lock | 读完整快照,不读半截 |
| run 写自己的 JSONL | 无 | 独占文件,无人竞争 |
| 写队列消费 PH 替换 / replace_range | session._lock | 写 main |
| 读 main_history.jsonl(replay 用) | 无 | append-only,完成后不再改 |

---

## 2. 渲染 —— fg / bg 双卡模型

### 2.1 两种卡

**[A] fg streaming card** —— 用户正在看的当前 run(永远 ≤ 1 张)

```
顶部 controls(永远固定,作为 anchor 防止页面跳动):
  ⏹ 停止             (针对当前 fg run)
  ➡ 后台             (手动 detach 当前 fg)
body:
  现有 P4 布局 —— thinking 折叠 / tool 折叠 / 回答 markdown / footer_meta
底部:
  模型给的选项按钮(若有);否则空
```

**[B] bg placeholder fold card** —— 每个被 detach 过的 run 共享的一张

```
header.title:
  初始:        🟡 正在后台 · {label}
  compact 后:  已压缩 N 条 · 🟡 正在后台 · {label}
  完成:        ✅ 回答完成 · {label}
body 元素(从上到下):
  1. text-progress 进度条
       compact 阶段刷 / 流式阶段刷 / 完成后填满
  2. collapsible_panel(默认折叠)
       初始 content:
            > 用户问: {original_prompt}
            > 助手: [此问题正在后台处理中,请基于其他可用上下文继续。]
       compact 之后(前部追加):
            ## 已压缩 {N} 条对话
            {compact summary}
            --- ↑ 分叉点 ↑ ---
            (原 PH 文本)
       ph_replaced 之后(替换 PH 文本段为):
            =以下为后台回答=
            {完整 run history: thinking / tool_use / tool_result / 助手回答}
无 controls。要操作 → 用 /attach。
```

### 2.2 Run 与卡的生命周期对应

```
状态机:
  submit
    └─→ fg streaming card 立刻出 [s1: fg-only]
  detach (用户点 ➡ 后台 / 发新消息 / 发 /attach / 发 /compact)
    └─→ fg 卡 freeze(controls 全删,body 不再编辑)
        + bg placeholder fold card 出(此 run 的首张 bg 卡) [s2: frozen-fg + bg]
  ph_replaced
    └─→ if bg 卡存在: 折叠块写入完整 history,标题转 ✅ 完成,进度条填满
        if 此时还是 fg(未 detach 过): fg 卡 finalize via coalescer
  attach(用户从 /attach 菜单点该 run)
    └─→ 当前 fg(若有) 走 detach 流程
        + 新 fg streaming card 出,从此 run 累积的 turns 重渲所有内容,继续接事件 [s3: 新 fg + 旧 bg]
        bg 卡保持原状(防重复)
  attach 后再 detach
    └─→ 新 fg 卡 freeze + 删 controls
        不再发 bg 卡(此 run 已经有 bg 卡了)

不变量:
  - 每个 run 最多有 1 张 bg 卡(首次 detach 时懒创建)
  - 每个 run 同时最多有 1 张 fg 卡
  - 整个 session 同时最多有 1 张 fg 卡(下一个 fg 出现前,前一个必须 freeze)
```

### 2.3 attach 流程

```
/attach 命令(triggers_turn=False,不进 inflight 跟踪):
  1. 列出所有 still-running PH:每个出一个 inline 按钮 [label + run_id]
  2. 用户点其中一个按钮:
     a. 当前 fg run(若有) → 自动 detach(走 §2.2 状态机的 detach 分支)
     b. gateway.attach(sid, target_run_id)
        - 旗子: target.detached = False; 其他 inflight.detached = True
        - emit attached(target_run_id)
     c. adapter 收到 attached:
        - 新 fg streaming card 出
        - 从 RunRenderState.turns(累积的 TurnDraft) 重渲所有内容
        - 后续事件按正常 render() 流程刷新这张新 fg 卡

不存在的 run / 已完成的 run:
  /attach 不列出(只列 running);已完成的从 bg 卡折叠块直接看
```

---

## 3. 命令与 detach 策略

### 3.1 规则

| 命令类型 | flag | 触发 auto-detach? | 渲染产物 |
|---|---|---|---|
| `/compact` | triggers_turn=True | ✓(走 fg 转 bg 路径) | compact 落在 bg 卡(详见 §4) |
| `/attach` | triggers_turn=False | ✓(实施新 fg 替换前先 detach) | attach 菜单 inline 卡 + 新 fg 卡 |
| `/help`, `/clear`, `/model`, `/effort`, `/thinking`, `/streaming`, `/search_provider`, `/project`, `/async`, `/resume` | triggers_turn=False | ✗ | 状态返回卡(命令卡) |

**唯一 auto-detach 的状态命令是 `/attach`**(它会替换 fg)。其他状态命令毫秒级返回,
不打扰当前流。

### 3.2 ⏹ 停止 vs ➡ 后台

```
⏹ 停止:
  gateway.cancel(sid, fg_run.run_id)
  → harness cancel(同 P5 设计:无工具止 LLM,只读工具立即 abort,写工具等 2s)
  → 写 partial 到 runs/{run_id}.jsonl + completion marker(status=cancelled)
  → 进入 PH 替换流程,bg 卡(若已存在)显示 "🛑 已取消"
  → 当前 fg 卡 freeze

➡ 后台:
  → 同发新消息的 auto-detach 路径,但不开新 run
  → fg 卡 freeze;bg 卡懒创建;run 在 bg 继续跑;无新 fg
```

---

## 4. Compact 整合

### 4.1 触发与 fork_point

```
触发:
  (a) /compact 命令(triggers_turn=True → auto-detach 当前 fg)
  (b) auto-compact(harness 内部 token 阈值)

fork_point = main 中最早的 running PH 位置 — 若无,= main 末尾
compact 跑 main[:fork_point] 内容,产 summary
写队列 replace_range(msg_ids 在 [0, fork_point) 区间, summary)
```

### 4.2 compact 卡定位 —— 落在哪张卡

```
case A: fork_point 处有 running PH(主流情况)
  → compact summary 写入这张 bg 占位折叠卡的折叠块前部
  → 标题前缀追加 "已压缩 N 条 · "
  → 不另发卡
  → 此 PH 后续被 replace 时:summary 留在前,实际答复跟随在后

case B: fork_point 处无 PH(纯 fg 一直在跑触发 auto-compact;或用户 /compact 但当下无 bg PH)
  → 新发一张 bg 占位折叠卡专门给这次 compact
  → 内容: 只有 compact summary(折叠块) + 进度条(完成填满)
  → 此卡不对应任何 user prompt;header 标题: "已压缩 N 条对话"
```

### 4.3 compact 进度条 / 标题更新

| 阶段 | 标题 | 进度条 | 折叠块 |
|---|---|---|---|
| 启动 | "🌀 正在压缩…" | 0% | (空 / 现有 PH 文本) |
| 进度回调 | 同上 | x% | 同上 |
| 完成(case A) | "已压缩 N 条 · 🟡 正在后台 · {label}" | 满格 | summary + 分叉线 + 原 PH 文本 |
| 完成(case B) | "✅ 已压缩 N 条对话" | 满格 | summary |
| 失败/取消 | "✖ 压缩失败" | 红色满格 | error 描述 |

---

## 5. 接口改动清单(需用户批准)

按 `feedback_interfaces_locked.md` 规则,本期需要批准的契约/接口改动:

### 5.1 contracts.py

**`memory/contracts.py`**:
- `Placeholder` 已存在,字段不变,无新增。

**`agent_adapter/contracts.py`**:
- `InflightRun.is_attached: bool = True` —— 区分"当前 fg"和"虽在 inflight 但已 detach 的 bg"。
  (现有的 `detached` 字段语义重叠,本期统一改名 `is_attached`,反义)
- `CommandResult.payload` 新约定键(不动 dataclass 结构,只是 dict 键约定):
  - `triggers_turn: bool = False`
  - `requires_detach: bool = False`(`/attach` 用)

### 5.2 interfaces.py

**`agent_adapter/interfaces.py`**:
- `AgentGateway` 新加抽象方法:
  ```python
  async def detach_foreground(self, session_id: str) -> None: ...
  async def attach(self, session_id: str, run_id: str) -> None: ...
  ```
  替换原 `switch_foreground`(语义不一致,本期不再用)。

**`memory/interfaces.py`**:
- `ActiveContextStore` 新加:
  ```python
  async def append_placeholder(self, session_id, agent_name, placeholder) -> None: ...
  async def replace_placeholder(self, session_id, agent_name, run_id, messages) -> None: ...
  async def replace_range(self, session_id, agent_name, msg_ids, summary_message) -> None: ...
  async def snapshot(self, session_id, agent_name) -> ActiveContext: ...   # 已有,语义补强
  ```
- `HistoryMirror` 同样补齐 `append_placeholder` / `replace_placeholder` / `replace_range`。

### 5.3 新 ProgressEvent.kind 值(字符串,不动契约)

- `attached`(替代旧 `foreground_changed`)
- `compact_landing_on_ph`(case A)
- `compact_new_card`(case B)
- 已有但语义补强: `run_started`, `run_detached`, `ph_replaced`, `compact_started`, `compact_finalized`

### 5.4 删除的接口

- `gateway.switch_foreground` —— 语义被 `detach_foreground` + `attach` 拆分替代
- `gateway.start_compact` 的 stub 路径 —— compact 走真实 harness 路径,不在 gateway 自产 events

---

## 6. 删除/废弃清单

| 旧物 | 处置 |
|---|---|
| 飞书 / TG 顶部"切回 {label}" 按钮 | 删 |
| `_send_full_card(is_new_message=True)` 中"切到 fg 即发新卡"的路径 | 改为 `attach()` 触发 |
| `build_card` 的 `bg_runs` kwarg | 删(没切回按钮) |
| `_make_send` 里 frozen 检查 + 防尾随 patch | 保留,仍要防尾随 |
| `build_frozen_ph_card` | 保留,detach 时给 fg 卡用 |
| `build_completed_bg_card` | 删(由 bg 折叠卡的 ✅ 完成 title 取代) |
| scripts/race_test/feishu_race.py 的切回断言 | 改为 detach + ph_replaced 断言 |
| 现有 e2e 测试 `test_feishu_race_e2e.py` 的 scenario_c_switch_back | 重写为 attach 场景 |

---

## 7. 并发推演表(主要场景)

| # | 场景 | 期望 |
|---|---|---|
| 1 | U1 → U2 快速 | fg1 出 → U2 触发 detach: fg1 freeze + bg1 出, fg2 出 |
| 2 | U1 → U2 → U3 | 同上级联:fg1 freeze+bg1, fg2 freeze+bg2, fg3 |
| 3 | U1 跑中点 ➡ 后台 | fg1 freeze + bg1 出;无新 fg(用户无当前流);run1 继续后台 |
| 4 | U1 跑完前 ⏹ 停止 | fg1 freeze 显 "🛑 已取消";run 写 cancelled 到 jsonl;bg1 不存在则不发 |
| 5 | U1 detach → ph_replaced | bg1 折叠块替换为完整 history,标题转 ✅ |
| 6 | U1 detach → /attach 选 U1 | 当前 fg(U2)走 detach 路径;新 fg(U1)出,从累积 turns 重渲 |
| 7 | U1 detach → /attach → U1 attach 后再被 U3 detach | U1 fg 第二次 freeze;不再发新 bg1(防重复) |
| 8 | U1 fg 期间 /compact | fg1 走 detach 路径(freeze + bg1 出);compact 落在 bg1(case A) |
| 9 | 没 bg PH 时 /compact(只有 fg) | fg 走 detach(出 bg1);compact 落在 bg1(case A) |
| 10 | 主上下文已空时 /compact | case B:新发 compact 专用 bg 卡 |
| 11 | 重启时存在 running PH | 启动扫 runs/;has completion marker → 直接 replace PH;否则保留 running 等 P5 resume |
| 12 | run completes 时仍是 fg | fg 卡自然 finalize via coalescer;bg 卡若存在则 ph_replaced 写入;若不存在跳过 |
| 13 | 别的 run 的 PH 在 LLM 视角 | snapshot 展开为合成 user/assistant 对话对 |
| 14 | sync 模式下 U1 在跑时 U2 | U2 入队等待;视觉上不出 fg2(因为 run 还没开跑);U1 完成后 fg2 才出 |
| 15 | 状态命令(/help) 在 fg 流期间 | 不 detach;命令卡独立出;fg 流不受影响 |

---

## 8. 不做(本期范围外)

- `/resume` 列表 + 恢复未完成 run(P5)
- 重启时跑一半 PH 的续跑逻辑(P5)
- /clear 改为新 session id + 旧 jsonl 保留(P5)
- Textual REPL 大改(Task 7 已落,本期不动)
- TG 单卡(Task 5 已落,本期沿用)—— bg 卡在 TG 表现为带"运行中" header 的单 message
- xanac REPL 的 attach UI

---

## 9. 验收后沉淀

属于本期最末一步:

1. **`best_agent/ds_review/ROADMAP/ASYNC_CHAT_DESIGN.md`**(新文档):把本 spec §1-§7 完整搬过去,作为下一阶段方案的权威源,替代旧的 ASYNC_RUN_DESIGN.md 渲染章节。

2. **`ASYNC_RUN_DESIGN.md`** 顶部加索引:"渲染与 PH 隔离的修订见 ASYNC_CHAT_DESIGN.md"。

3. **xana `docs/design/`**:更新 `bg-streaming.md` + `compact-rendering.md` 引用本 spec。

4. ROADMAP 里 P6 🟡 段:标 ✅ 已落地,指向本 spec。

5. 一次 commit,message:`docs: sink async-chat design (PH-first + dual-card) to ASYNC_CHAT_DESIGN.md`

---

## 10. 任务分组(供 writing-plans 用)

```
Group A — 契约 / 接口批准 + 增改(阻塞)
  - ActiveContextStore: append_placeholder / replace_placeholder / replace_range / snapshot
  - HistoryMirror 同上
  - AgentGateway: detach_foreground / attach
  - InflightRun.is_attached
  - 删 gateway.switch_foreground
Group B — main 上下文 PH 写入 + snapshot fork
  - submit 流程改写: snapshot → append user → append PH → enqueue fork_ctx
  - prompt 装配把 Placeholder 展开为合成 user/assistant 对话对
Group C — write queue + consumer
  - boot 启动单 consumer
  - replace / replace_range 入队消费 PH 替换 / compact 写入
  - sync vs async 区别仅在调度
Group D — gateway detach_foreground / attach
  - detach_foreground: 标 detached,emit run_detached
  - attach(run_id): auto-detach 当前 fg,emit attached
  - 删 switch_foreground
Group E — 飞书 PH-first 双卡渲染
  - fg streaming card 顶部 controls (⏹ + ➡)
  - bg placeholder fold card(进度条 + 折叠块)
  - 懒创建 bg 卡的状态机
  - attach 时新 fg 卡 + RunRenderState.turns 全量重渲
Group F — TG 同步双卡(单 message 编辑)
Group G — CLI/Textual 同步双视图(StdoutRenderer 折叠输出 / TextualRenderer pane 切换)
Group H — Compact 写队列 + 落卡逻辑(case A/B)
  - replace_range 通过写队列
  - case A 写入现有 bg 卡折叠块
  - case B 新发 bg 卡
Group I — /attach 命令(列举 running PH + inline 按钮)
Group J — ⏹ 停止 / ➡ 后台 按钮接管 fg controls
Group K — 清理废弃代码
  - 飞书 / TG switch_back button
  - build_card.bg_runs kwarg
  - build_completed_bg_card
  - test_feishu_race_e2e.py scenario_c 重写为 attach
Group L — 真 e2e:扩 scripts/race_test/feishu_race.py + tests/test_feishu_race_e2e.py
  - 场景 1-12 全跑(对照 §7 表)
Group M — 用户验收闸(手测飞书 / TG)
Group N — 沉淀(§9)
```

---

## 11. 风险

- **PH 展开成合成 user/assistant 对话对** 在 prompt 装配层是新点 —— DefaultHarness 现有装配
  没有这一步,要小心改的位置(`_assemble_prompt` 或同级)。
- **InflightRun 改名** 影响范围大(gateway / commands / 飞书 / TG / CLI / 测试 fixture 都用)。
- **写队列 consumer 的并发** —— run consumer 和 write_queue consumer 必须是 **两个独立 task**,
  否则 sync 模式下 run consumer 在 await _invoke,_invoke 完成入队,但没人消费 → 死锁。
  boot 启动两个独立 loop:一个跑 input_queue → _invoke,一个跑 write_queue → 替换 main。
