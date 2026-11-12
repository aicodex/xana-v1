# Session / Run / Card 状态机(终极版)

> 本文档是 async + 卡片所有行为的**最终来源**。docs/superpowers/specs/ 下早期 spec 之间互相打架,以本文为准。
> 修改本文 = 修改契约,必须先经用户确认。`tests/e2e_real/test_card_state_contract.py` 是本文的可执行断言,merge gate 必跑,**不得改测试糊弄**。

---

## 0. 总原则

- **异步与同步都走异步**。所谓"同步"只是异步 + 阻塞了消息消费队列的特例。包括用户消息等待队列在内,**所有 Run 都是从主会话 fork 出来的后台异步 run**。
- **只维护一种卡**:统一 unified MVVM 分块卡(UnifiedCardState)。各 channel(飞书 / TG / CLI)是不同 View,**共用同一 Model + ViewModel**,根据元素是否存在、状态如何来显示/变化。

---

## 1. 会话状态(后端)

### 1.0 会话身份与持久化(session_id 来源)

**不存在"默认"会话 id**(没有 `default` 等特殊路径)。所有 channel 都用同一套映射:

```
key = (adapter_id, thread_id) → session_id
```

| adapter | thread_id 取值 |
|---|---|
| feishu | `chat_id`(单聊 = open_id;群聊 = chat_id) |
| telegram | `chat_id` |
| cli | `""`(CLI 是单 client,一个 host 一个 thread) |

`system.json` 持久化整张 `key → session_id` 映射表。运行时:
- 入站消息到达 → adapter 查 `system.json` 拿 session_id;查不到 → 生成新 session_id + 写回
- /clear → 直接换新 session_id 写回 `system.json`,旧 session 整个保留在内存里
- 想"回到旧会话" → 手编 `system.json` 把 key 改回旧 session_id 即可

**数据权威:内存是唯一 source of truth。** `system.json` 跟 `history/` 一样是**副本写(mirror write)**:
- 写路径:写内存 → 异步 mirror 到 `system.json` / `history/`
- 启动恢复:内存为空 → 从 `system.json` + `history/` 读副本重建
- **磁盘不能反向覆盖内存** —— 运行中不再读 `system.json`(只在启动时读一次),避免外部编辑导致内存与磁盘不一致

### 1.1 主会话上下文(main context)

- 任何用户的问题、回复**在主上下文里默认就是 PH**(分叉占位符)。也就是说:用户消息进入 → 立刻给该 turn 创建一个 PH 节点占位,真正的回答 / 工具 / 思考都在后台 Run 里跑,Run 完成时**回写**这个 PH。
- 主上下文**维护分叉点**。一旦后台 Run 回写完成,该分叉点(PH)就消失 —— 被替换为完整上下文(用户消息 + tools + 思考 + 回答 等真实节点)。
- 主上下文通过**锁等机制**防止 compact / 回写 / fork 并发时的同时读写。

### 1.2 子线程上下文(每个 Run 的 prompt)

- 子线程的上下文 = **fork 那一刻主上下文的快照**(子的 PH 是"先复制后写入" —— fork 时主里还没它自己的 PH,fork 之后主才追加自己的 PH;子永远看不见自己的 PH)。
- **后来的子线程,会复制到前一个子线程已经留在主里的 PH**(因为前一个 Run 没回写时,主里那个 PH 仍在)。
- 这些"看得见的别人的 PH",通过 [`ph_expand`](../../src/xana/harness_sdk/ph_expand.py) 展成 user/assistant 合成对喂给 LLM。

### 1.3 后台占位符内容(PH 节点的可见 content)

**普通 PH 展开后只有一条 assistant 消息**。模型看到的就像它自己直接回答了一句。**逐字**(纯文本,无任何 run_id / UUID / 内部字段):

```
assistant: 正在后台回答你的问题{ph.label}，请勿重试此请求。
```

例如 `label="review xana"` →

```
assistant: 正在后台回答你的问题review xana，请勿重试此请求。
```

核心目的:让**后来的子线程看到主里这个 PH 时,知道这事正在后台处理,不要重复运行**。run_id 等元数据**只在 store 内部维护**,序列化给模型时全部剥离(详见 §11.1 TurnMessage 序列化器)。

### 1.4 Run 的运行不影响前台

- 所有 tools / 流的处理都在**后台 Run 里直接进行**,响应到那个 Run 的 RenderState。
- **前台卡片仅同步状态**(订阅当前所属 Run 的事件,patch 卡)。
- 后台 Run 与当前前台 Run 各自独立,照样跑、照样产生事件 —— 只是不被前台卡片渲染(其对应卡处于 frozen)。

### 1.5 compact 的两种触发路径

**A. 没有任何后台正在运行**
- 直接发起一个空会话(创建占位符)
- compact 段单独占用这张新建卡片

**B. 自动或手动触发,但**有**后台在运行**
- 找主上下文里的 PH 点
- **把 PH 点之前的用户问题也压缩进去**
- 完成后,**把 PH 点之前的内容替换为压缩结果**(等于主上下文从开头到该 PH 之间全被摘要节点替换)

### 1.6 子会话的 compact(子 Run 内部触发的 compact)

- **第二次压缩不再读主上下文了**(已经有过自己的压缩结果,直接接着压自己积累的新内容)
- **首次压缩**只看一件事:它能看到的、**最靠前的分叉点,它前一个消息一定是用户问题**。
  - 情况 A:那个"前一个消息"已经**变成 compact 结果** —— 说明已经压缩过了,**直接用这个 compact 结果**(跳过自己压缩)
  - 情况 B:那个最靠前的分叉点**此时此刻已经回写**(不再是 PH 了) —— **看下一个**最靠后的分叉点,它一定是**自己的那个 PH**(因为自己肯定还没结束,不然不可能由自己触发 compact);此时由自己压缩
- 主上下文锁保护:查 + 用 + 写要原子。

### 1.7 /clear

- = **换 session_id**(对当前 `(adapter_id, thread_id)` 生成新 session_id,写入 `system.json` 副本)
- 旧 session 的内存(active_context + memory tree + bg run)**全部保留在内存里**,不清不杀
- /clear 语义 = 换绑:旧历史依然在内存里,当前 thread 改绑到新 session_id
- **/clear 那一刻和所有菜单消息一样,触发当前前台卡 detach**(走 §4.7 的 detach 路径)
- 旧会话对应的**后台 Run 暂不处理,让它继续跑**(它们写回旧 session 的内存,不影响新 session)
- 旧卡保持在它当前的形态;新会话有自己的新卡
- 想回到旧会话:手编 `system.json` 把 `(adapter_id, thread_id)` 指回旧 session_id,**重启 serve** 生效

### 1.8 attach / detach 的范围 + 触发方式

- **attach 和 detach 只对前端卡片样式有用**。
- **对后端(Run、上下文、PH、回写)不应该产生任何影响**。后台 Run 继续跑、PH 继续等回写。

**detach 触发方式只有两条:**
1. **"后台回答"按钮**(顶部按钮)被点
2. **任何菜单消息 / 命令 submit**(包括 /compact、/clear、/help、/attach、普通用户消息)

> 没有"detach 菜单"。submit 任何东西就等于 detach。

**/attach 流程:**
- submit `/attach` (无参数)→ **此时此刻**判断后台候选 Run → 弹出后台列表卡(`build_attach_menu`)
- 用户点列表中某项 → submit `/attach <run_id>` → **新建一张 fg 卡**(新 card_id + 新 message_id)
- **即使目标 Run 已经 terminal,attach 依然新建一张卡**显示其终态。
  - 即:attach 永远新建卡,从不"复活旧卡"。
- 旧 fg 卡(如有)同时被 detach 走 frozen 路径。

---

## 2. Run 状态机

```
                  submit (fork from main snapshot)
                       │
                       ▼
              ┌─────────────────┐
              │   streaming     │  后台跑,事件流持续
              │   (前端 fg)      │  前端卡 = 此 Run 的实时镜像
              └────────┬────────┘
                       │ 用户发任何消息 / 菜单 / /compact
                       │ → 前端按钮 = "后台回答" 同效
                       ▼
              ┌─────────────────┐
              │   streaming     │  后台继续跑(不受影响)
              │   (前端 bg)      │  前端卡 frozen,不再更新
              └────────┬────────┘
                       │ Run 完成 → ph_replaced
                       ▼
              ┌─────────────────┐
              │    terminal     │  ph_replaced 一次性 patch
              │                 │  把最终内容写回那张原卡
              └─────────────────┘

             用户点取消回答 → /cancel <run_id>
                       │
                       ▼
              ┌─────────────────┐
              │    cancelled    │
              └─────────────────┘
```

**关键:Run 自身的状态(streaming → terminal)只由后端事件驱动。前端 detach 不杀 Run、不影响其状态机。** 前端 fg/bg 只是"这张卡是否还订阅这个 Run 的事件流"的差别。

---

## 3. 卡片(MVVM):一个模板,多个 View

### 3.1 模板定义

无论飞书 / TG / CLI,**只维护一个"卡片模板"**。它定义:
- 所有可能出现的元素(section)
- 各元素的状态(streaming / detached / terminal / cancelled / completed / 等)
- 元素的**先后顺序**(固定)

各 View 根据"有没有相应元素 / 处于哪些状态"显示和变化。

### 3.2 Section 顺序(自上而下,固定)

```
1. 压缩进度条(一行文字,带 ▰…▱ 进度)
2. 压缩下拉                       ┐
3. 思考过程下拉                    │  panel 段
4. 工具下拉(默认 3 个) + 展开按钮 │
5. 回答块(主答案,可折叠/展开)     ┘
6. 脚标(footer)
7. 顶部按钮(取消回答 / 后台回答 / 取消)
```

> 注:button row 实现上挂在 top_buttons / bottom_buttons,但视觉位置由 Channel View 决定(飞书 top 在最顶,TG 多用 bottom inline_keyboard)。

**为什么 compact 结果排在 answer 前**:因为 compact 是对该 PH 点之前的总结,回答出现在 PH 之后。语义上"前置摘要 + 后续回答" → UI 顺序也是"摘要在上,回答在下"。

### 3.3 MVVM 分层(责权)

| 层 | 提供方 | 内容 |
|---|---|---|
| **Model** | `xana.agent_adapter.channels.unified_card.UnifiedCardState` 等 dataclasses | 所有 section 字段 + 状态 enum + 派生小工具(append_*, advance_*, finalize_*) |
| **ViewModel / Event Bridge** | `xana.agent_adapter.channels.unified_card_events.apply_event` | 从后端 `HarnessEvent / AgentEvent / ProgressEvent` 推 Model 状态。**唯一**的状态推进入口。adapter 只负责把事件喂进来、把 state 喂给 renderer。 |
| **View(平台)** | `unified_card_feishu.render` / `unified_card_telegram.render` / `unified_card_cli.render` | 把 `UnifiedCardState` 渲染成平台 schema(飞书 dict / TG MarkdownV2+keyboard / Textual widget) |
| **Agent 侧接口** | `xana.harness_sdk` 的 HarnessEvent 流 + `Placeholder` 的 prompt 展开 | Agent 不直接见 Card;它只发事件。Card 的存在对 Agent 透明。 |
| **Adapter** | `FeishuAdapter / TelegramAdapter / CliAdapter` | 1) 维护 card_id ↔ message_id ↔ run_id 绑定 2) 调 apply_event 推 Model 3) 调 renderer 出 schema 4) 决定 send vs patch 5) 处理按钮回调路由 |

**绑定上下文状态**:卡片选择上**绑定 PH/上下文**。例如"前台 Run 正在跑"时,这张前台卡本身在主上下文里也是一个 PH 占位 —— 直到回答写完(Run 完成 → ph_replaced),它的回答 section 才从"流式中"变成"终态"。compact 结果出现在前面(PH 之前的内容被摘要替换),回答出现在 PH 替换后的位置(自然就是下方)。

---

## 4. 卡片元素详细规格(以飞书为基准,差异点单列)

### 4.1 压缩进度条 "压缩中"

- **10 段**(▰/▱)
- **7 段输入阶段**:**3s 进一格**(用户口述明定;只是估算,不准 —— 实际推进时间随便)
- **如果模型开始输出了 → 直接跳到 7**
- **3 段输出阶段**:根据 compact 输出的 N 个 section 进度算

实现:[unified_card.advance_compact_input / advance_compact_streaming](../../src/xana/agent_adapter/channels/unified_card.py)

### 4.2 压缩下拉(折叠面板)

- streaming 时:title "压缩中",body 已积累的摘要片段
- completed 时:title "已压缩 N 条消息",body 完整摘要
- cancelled 时:title "压缩已取消"

### 4.3 思考过程下拉

- 默认折叠
- 标题:有内容 → "❋ 思考过程";空 → "❋ 思考中"
- body = 模型 reasoning 流

### 4.4 工具下拉(tool blocks)

- **默认 3 个 visible**(`TOOLS_VISIBLE_LIMIT=3`)
- 每个 tool 是一个折叠面板
- header 文本规则:
  - **bash**: 显示 LLM 输出的**短标题**(prompt 里要求模型给 `description`,即 [`_tool_summary`](../../src/xana/agent_adapter/channels/unified_card_events.py) 的 1)
  - **其余工具**: 显示**参数**,优先级固定:**description > path / file_path / file / cmd / command / url / query / pattern > 第一个非空 kv**(用户已确认)
  - 完整头格式:`✎ {name} · {summary} · {elapsed} · {status}`(status ∈ ok/fail/cancel)
- body 渲染:
  - 飞书:截 800 字符上限(单卡 30KB 总限,避 200800)
  - TG:**top 3 行 + 每行最多 60 字**(用户口述明定)
- **超出 3 个时**:
  - **飞书**: 显示"展开 ✎×N"**按钮**(N = 未扩展个数 = `tools_pending = len(_pending_tools_buf)`)
  - **TG**: 显示"展开 ✎×N"**文字提示**(非按钮 —— 用户口述明定)
  - 点击/触发(飞书)→ `load_pending_tools`: buf → tools, pending=0, 按钮消失
  - 后续又来新 tool → 又进 buf → 按钮/提示重新出现

### 4.5 回答块

- **fg 默认展开**
- **bg 默认折叠**(用户口述"bg 合上")
- 标题状态:
  - streaming 有内容 → "✦ 回答";空 → "✦ 回答中"
  - cancelled → "✦ 回答已取消"
- **内容渲染:只有回答渲染 markdown**(其他段落是纯文本)
- streaming 截断处理:**必须考虑最后截断语法不全的问题**(`*x` 缺闭合 `*`、表格半行、未闭代码块等)→ View 层负责修补/截到安全 boundary。**已实现,主要在 TG 侧**(走 telegramify-markdown)。飞书侧由于自身 markdown 容错较好,不需要额外守门。

### 4.6 脚标(footer)

固定四形态(实现:[`FooterText.render`](../../src/xana/agent_adapter/channels/unified_card.py)):

| Run / Card 状态 | 脚标 |
|---|---|
| streaming(fg) | `🟡 model · 上下文 Nk/Mk · 回答用时 53s` |
| terminal(完成) | `🟢 model · 上下文 Nk/Mk · 回答用时 53s` |
| cancelled | `🔴 model · 上下文 Nk/Mk · 回答用时 53s` |
| bg(frozen) | `🟡 正在后台回答` |

**互斥规则**(用户口述明定):
- "🟡 正在后台回答" 与 "运行中 / 结束的 3 段内容" **互斥** —— 即 bg_frozen 态下绝不出现 model·ctx·回答用时三件套。
- 三件套(model + ctx + 回答用时)**绑定**:任一字段缺失,整段不渲染。
- **工具用时(菜单段)永远排最尾**:例如 `compact 用时 10s` 跟在所有内容之后,即便 bg_frozen 也允许尾巴挂 menu 段(解决 compact + 后台 竞态)。

### 4.7 顶部按钮

| Run 状态 | 按钮 |
|---|---|
| streaming(fg) | "取消回答" + "后台回答" |
| streaming(bg, 仍未完成) | "取消回答"(允许取消后台 run) |
| compact 单独运行(answer 不活跃) | "取消"(`/compact_cancel`) |
| terminal / cancelled | 无 |

- **点击"后台回答"** → 前端卡片转 bg 状态(detach),卡片样式变 frozen
- **发送任何消息 / 任何菜单选项**也会导致 dettach(等价行为)
- **不需要专门写一个"dettach 菜单按钮"** —— "后台回答"按钮 + "submit 任何东西"已覆盖
- 按钮 callback **建议以 run_id 编号**(尽管前台同时最多一个 Run,但形式上 run_id 让 callback 是自描述、可路由的)

---

## 5. bg(frozen)状态的精确语义

- **卡片不再动**(不更新内容、不更新工具 panel、不更新按钮样式)
- **直到回答结束**(后端 Run terminal → ph_replaced)
- 此时**一次性刷新成"和流最后一刻一样"**(即此时此刻 Run 的完整 RenderState 一次性 patch 进卡)
- 期间后台 Run 的所有事件全部累积在 Run 自己的 RenderState 里,不写卡(adapter `_deliver` 被 `is_frozen` 门挡住)

### TG 与飞书的 bg 差异

- 飞书 bg:tools_pending 提示是**按钮**(但 detached 态下渲染成静态文字 "_后台 ✎ 中_",不可点)
- TG bg:tools_pending 提示**本来就是文字**(非按钮),所以 detach 前后视觉无变(都是"展开 ✎×N" 文字)

---

## 6. TG 与飞书的渲染差异(总表)

| 项 | 飞书 | TG |
|---|---|---|
| 折叠 panel | `collapsible_panel` 灰底 + chevron | expandable blockquote(`**>` + `||`) |
| 工具 body | 截 800 字符 | **top 3 行 + 每行 60 字** |
| tools_pending 提示 | 按钮"展开 ✎×N" | **文字"展开 ✎×N"**(非按钮) |
| 超长 thinking / answer / tools | 直接折叠,展开看全 | 底部多出 3 个按钮:"展开 思考 / 工具 / 答案"(全量) → 点击进**全屏单段卡**,支持**翻页** |
| 翻页粒度 | 无 | **tools 一页最多 4 个**;answer 按 TG 单消息长度切页 |
| markdown 渲染 | 只回答用 markdown | 只回答用 markdown;TG 走 telegramify-markdown,**answer 必须考虑流式截断时元素不闭合**(`*` `_` `` ` `` ``` 等末尾补闭/截到安全 boundary) |
| 命令 linkify | 无 | `linkify_commands=True` 时正文 `/cmd` 原生可点 |

CLI 见 [channels/cli.md](./channels/cli.md)。

---

## 7. 关键事件 → 状态转换(端到端)

### 7.1 首次 submit("你好")
1. main 追加 `MemoryMessage(user, "你好")`
2. 创建 Run R1,从主**当前快照** fork(R1 看不见自己的 PH —— PH 还未追加)
3. main 追加 `Placeholder(R1, label="你好")`
4. 新建 Card C1,首次 send → 获得 message_id
5. R1 后台跑,事件流 → apply_event(C1) → patch C1
6. R1 完成 → `ph_replaced` → `apply_ph_replaced(C1, final_answer=...)` → terminal

### 7.2 streaming 中用户再发("再问一条")
1. **前端 detach C1**(`apply_detach(C1)`):C1 patch 一次 → frozen,**后端 R1 不受影响,继续跑**
2. main 追加 `MemoryMessage(user, "再问一条")`
3. 创建 R2,fork main(此时主里已有 R1 的 PH;R2 prompt 通过 ph_expand 看到 R1 PH)
4. main 追加 `Placeholder(R2)`
5. **新建 Card C2**(新 card_id + 新 message_id)→ R2 streaming patch C2
6. R1 完成 → ph_replaced 找到 C1(绑定)→ patch C1 终态,`terminal_from_bg=True`
7. R2 完成 → patch C2 终态

### 7.3 /compact 中途介入(Scenario 2 核心)
1. R1 review 正在 streaming,C1 是当前 fg
2. 用户 submit `/compact`
3. **前端 detach C1**:C1 patch 一次 → frozen(后端 R1 不受影响)
4. 创建 compact Run Rc(`triggers_turn=True`,但**不**追加 user 节点)
5. 找到"**主上下文中最早未完成 PH**" → R1 的 PH → **挂在 C1 上**
6. `apply_event(compact_started)` → C1 多 compact 段;footer 末尾出现 "compact 用时 t"(menu_label="compact")
7. compact 进度推进:input(7 格,~3s/格,模型开输出跳 7)→ streaming(7→10,按 sections)
8. compact 完成 → finalize_compact → "已压缩 N 条消息";**主上下文 PH 点之前的内容被替换为摘要**
9. R1 继续跑;完成时 ph_replaced(R1)→ C1 answer 段 patch 终态;**C1 同时承载 compact 段 + answer 终态**
10. 用户下一条消息走 7.2 → 新建 C2

> compact **不**开新卡(除非 1.5.A 无后台空闲态)。

### 7.4 /attach
1. 用户 submit `/attach`(无参数)→ **此时此刻**判断所有后台候选 Run → `build_attach_menu` 出一张菜单卡
2. **默认 highlight 当前 detached 的那个**
3. 用户点列表某项 → submit `/attach <run_id>`
4. 流程:
   - 当前 fg(如有)→ detach(同 7.2 步 1)
   - **新建 Card Cn** 绑定到目标 Run
   - 此后 Cn 接收该 Run 的事件;旧 card 保留冻结态
5. **即使目标 Run 已经 terminal(后台已结束),attach 依然新建一张卡显示其终态。从不复活旧卡。**
6. 后端 Run 状态不动

### 7.5 /cancel
- top button "取消回答" → submit `/cancel <run_id>`
- 后端真取消 Run;Card 走 cancelled 形态(`cancel_answer`,footer 红点)
- 后续 delta no-op

### 7.6 /clear
- **触发 detach**(同 7.2 步 1)→ 当前 fg 卡冻结
- 主会话换新 session_id(切语境)
- 旧 session 的后台 Run **不杀**,让它跑完(回写到自己原卡)
- 新 session 起一张新卡显示新对话

---

## 8. 不变量(I1–I17,merge-gate 测试断言)

| # | 不变量 | 关联条 |
|---|---|---|
| I1 | 1 Run ↔ 1 PH ↔ 1 Card 终生绑定 | §1.1, §2 |
| I2 | Card 一旦 send,后续都 patch 同 message_id,不重发覆盖 | §3.3 |
| I3 | 前端 detach / attach 不改变后端 Run 状态、不杀 Run、不影响 PH | §1.8 |
| I4 | submit 任何消息(含 /compact)→ 当前前台卡立即 detach | §4.7 / §7.2 |
| I5 | /compact 段挂在"主上下文最早未完成 PH 所属的 Card"上,不新建卡(除非空闲态) | §1.5 / §7.3 |
| I6 | /attach 总是新建卡 | §1.8 / §7.4 |
| I7 | bg(frozen)Card 从 detach 到 ph_replaced 之间**零更新**;ph_replaced 一次性 patch 成最终状态 | §5 |
| I8 | tools_pending 只数未展开 tools(buf 长度);load_pending_tools 后归零 | §4.4 |
| I9 | footer 三件套(model + ctx + 回答用时)绑定;缺一不渲染 | §4.6 |
| I10 | bg_frozen 脚标永不出现 "model · ctx · 回答用时" 三件套(互斥) | §4.6 |
| I11 | 任何 channel View 对同一 UnifiedCardState 产出语义一致的输出 | §3.3 |
| I12 | 子会话 compact:首次查"同分叉点已压缩可用就用",二次起不再读主上下文 | §1.6 |
| I13 | session_id 来源于统一映射 `(adapter_id, thread_id) → session_id`;**禁止任何 channel 走"default"等特殊路径** | §1.0 |
| I14 | thread_id 取值固定:feishu/tg = chat_id;cli = `""` —— 同一 thread 在同一 channel 只对应一个 active session_id | §1.0 |
| I15 | 内存是 session_id 映射的**唯一 source of truth**;`system.json` 是副本写(mirror write),`history/` 同理 | §1.0 |
| I16 | `system.json` **只在启动时读一次**;运行中不再读,避免外部编辑悄悄覆盖内存。手编需重启生效 | §1.0 |
| I17 | /clear = 对当前 `(adapter_id, thread_id)` **换 session_id**(不清旧内存);旧 session 的 active_context / memory tree / bg run 全部留底,可通过手编 `system.json` 重启回归 | §1.7 |

---

## 9. 测试合约(committed,禁止借口"是别人引入的")

参见 `tests/e2e_real/test_card_state_contract.py`(待落)。
- 仅 `pytest -m merge_gate` 跑(默认 skip,不拖日常 CI)
- 失败 = 行为偏离本文 = **必须修代码**,**不得**借口"是别人引入的回归" —— 没有别人,只有我
- 修改本测试 = 修改契约 → 必须先改本文 + 用户确认

---

## 10. 已确认决议(对应早期开放点)

| # | 决议 |
|---|---|
| §4.1 | 输入推进 3s/格(估算,不精确) |
| §4.4 | 非 bash 工具 header:description > path/file_path/file/cmd/command/url/query/pattern > 第一非空 kv |
| §4.5 | streaming markdown 截断守门:**已实现,主要在 TG**(telegramify-markdown),飞书不补 |
| §1.6 | 子会话首次 compact 查询:看最靠前分叉点的前一条 —— compact 结果就用;否则它已回写就看下一个(必是自己 PH)→ 自己压 |
| §1.7 | /clear 触发 detach |
| §1.8 | detach 只有"后台回答"按钮 + 任何菜单/命令 submit 触发;attach 永远新建卡(目标已 terminal 也新建) |
| §6  | TG 比飞书多 3 个按钮(展开 思考/工具/答案 全量),tools 翻页每页 4 个;answer markdown 不闭合守门 |

---

## 11. 待落地重构(用户已确认,排队中)

### 11.0 上下文操作 SDK(只允许一个入口,代码层强制)

**问题**:今天的代码里到处都是直接 `self._bindings.allocate()` / `self._bindings.put()` 等"旁路接口" —— 同样的语义有多种调用方式,resolver 加进来还没能彻底替换 BindingStore,典型 split brain 的根因。

**目标**:把所有上下文/会话操作变成一个 SDK 接口对象,**代码层禁止任何旁路调用**:
- 收敛 ActiveContextStore + MainWriter + SessionResolver + BindingStore 到一个 facade(如 `ContextSDK`)
- 各 adapter 只持有 `ContextSDK` 句柄,直接字段访问其内部组件就报错(用 `__slots__` 限制 / 只 expose 方法 / runtime check 调用栈)
- 不再有"resolver 没传就 fallback BindingStore"的可能性 —— **没 SDK 就 ImportError 启动失败**
- 所有 channel(feishu / tg / cli)、所有命令处理(/clear / /compact / /attach)、harness 都走同一个 SDK

落地时机:在 §11.1/11.2 之后做,基础类型(TurnMessage)清晰后再整。

### 11.1 TurnMessage 类 + 平台序列化器

**问题**:当前 `MemoryMessage` / `Placeholder` 直接被 ph_expand 展开成纯 dict,送给模型。PH 的 `run_id` / 内部元数据可能泄漏到 prompt,导致模型推测出"上下文 = jsonl 路径 = 我可以读取"。

**重构契约**:
- 定义 `TurnMessage` 基类(role + 文本 + **内部元数据**)
- 内部元数据(run_id, ph_run_id, message_id, debug fields...)**在序列化时全部剥离**,模型 prompt 永不携带
- 实现两个 platform serializer:
  - `serialize_for_openai(turn_messages) -> list[dict]`
  - `serialize_for_anthropic(turn_messages) -> list[dict]`
  - 两家协议略有差异(tool_calls 嵌套形态、system 角色处理等)
- Placeholder 在序列化时变成 `MemoryMessage(role="assistant", content="[正在后台回答 '{label}',不要重复尝试运行此内容。]")` — **无 run_id,无 UUID**
- 模型看到的 PH = 普通 1:1 聊天的一句 assistant 回复,**0 区别**

**测试合约(待写)**:
- I18: 序列化结果中**禁止**出现任何 run_id / UUID / 内部字段
- I19: PH 序列化文本必须以 "正在后台回答" 起,不含 run 标识

### 11.2 Compact-as-PH(方案 C,已选定)

**问题**:今天 `_cmd_compact` 用 `snapshot()` 读 main + 算 `fork_point` + 调 `replace_range(0, fork_point)`,完全是 race-prone:
- 如果 compact 跑的时候,目标 PH 已经被 `replace_placeholder` 替换掉了 → 找不到 running PH → `fork_point=len(mixed)` → **把刚回写的回答也吞了**(就是 16:50:59 那条 `replace_range[0:9]`)
- 其他 in-flight run 完全看不到 compact 在跑;他们 fork 时拿的 main 跟 compact 后的 main 不一致 → 回写位置丢失

**新协议(C):compact 自己是个 PH**,统一进 PH replace_placeholder 协议:

#### 4 条强制约束(用户确认)

1. **Compact PH 隐身** —— 任何模型读上下文时,compact PH **0 条**(普通 PH 是 1 条 assistant)。纯透明,等于少一条消息。
2. **插入位置** —— compact PH 永远插在"main 里**最靠前 regular PH** 的前面"。
3. **读取协调** —— 任何新 run 在 fork main snapshot 之前必须检查:
   - 最靠前 regular PH **再前面**有 compact PH 吗?
     - **没有** → 直接 fork(常规)
     - **有,但已被 replace** → 读到的是 summary,直接 fork(常规)
     - **有,且还在 running** → **阻塞等待**,直到 compact PH 被 replace 后再 fork
4. **会话级单实例 compact 锁** —— 同一 session_id 任意时刻**最多一个** compact 在跑(`asyncio.Lock` per sid)。无论代码 bug / 双触发 / 并发,都序列化。

#### 关键点

- **写回靠 run_id,不靠位置**:既存 `replace_placeholder(run_id, ...)` 已经按 run_id 找 PH。compact 用同一 API 写回自己,完全不动其他 PH。
- **compact PH 的 content**:占位时随意,reader 看不到。replace 时换成 `MemoryMessage(role="compact", content=<summary>)` —— 这条要被后续 fork 看到。
- **多 compact 防御**:由于约束 4 锁,理论上不会有"两个 compact PH 同时存在",但 reader 还是要按"最早 compact PH"找,做 belt-and-suspenders。
- **取消**:compact 取消时 `replace_placeholder(compact_run_id, ())` —— 替换为空,等于撤销 PH。reader 看到空替换,跟没 compact 一样。

#### 接口改动(草案)

- `Placeholder` dataclass 加 `kind: Literal["regular", "compact"] = "regular"`
- `ph_expand`:遇到 `kind=="compact"` 直接 **skip**(不展开)
- 新方法 `ActiveContextStore.snapshot_for_fork(sid, agent) -> tuple[...] | "wait"`:
  - 内部检测 compact PH running → 返回信号让 caller asyncio.Event 等
  - 检测完则返 mixed snapshot,删掉已 replaced 的 compact PH(让 fork 看到 summary 直接接续)
- `_cmd_compact`:
  - 拿 sid 锁
  - 找最早 regular PH 位置 K
  - `append_placeholder(Placeholder(kind="compact", ...), position=K)` —— 需 active_context 支持指定位置插入
  - 启动 compact run,跑完调 `replace_placeholder(compact_run_id, (summary_msg,))`
  - 释放锁

### 11.3 待修 bug(以日志验证)

- **B1**: PH 被 `replace_placeholder` 两次 — **复盘后确认是误判**:1 次 write + 1 次紧跟的 read snapshot(标签糊涂导致看着像双写)。新日志加了 WRITE / READ 区分,不再有歧义。
- **B2**: /compact 摘要包含 /clear 之前的内容 — 跨 session 内存污染,需复现追溯到底是哪个 store 读的。
- **B3**: /compact **吞掉了 PH 后面的回答**(日志 16:50:59 `replace_range[0:9]` 已确认根因)—— 见 §11.2 方案 C,通过把 compact 装进 PH 协议来彻底消除 race。
