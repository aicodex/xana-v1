# Session / Run / Card 状态机(终极版)

> 本文是 async + 卡片 + 上下文 所有行为的**唯一来源**。
> 文档只描述**当前正确设计**,无历史、无对比、无"旧/新"。
> 修改本文 = 修改契约,先经用户确认。`tests/e2e_real/test_card_state_contract.py` 是本文的可执行断言,merge-gate 必跑。

---

## 0. 总原则

- **异步与同步都走异步**。所谓"同步"只是异步 + 阻塞了消息消费队列的特例。所有 Run 都是从主会话 fork 出来的后台异步 run。
- **只维护一种卡**:统一 unified MVVM 分块卡(`UnifiedCardState`)。各 channel(飞书 / TG / CLI)是不同 View,共用同一 Model + ViewModel。
- **上下文操作只有一个入口**:`ContextSDK` facade。代码层强制(`__slots__` + ABC + 没 SDK 句柄就启动失败)。禁止任何 adapter / harness / 命令处理代码直接调 `_store` / `_bindings` / `_resolver` 等内部组件。
- **内存是唯一 source of truth**。`system.json` 与 `history/` 都是**副本写**(mirror write);启动时读一次重建内存,运行中永不再读磁盘。

---

## 1. 会话状态(后端)

### 1.0 会话身份与持久化(session_id 来源)

所有 channel 走同一套映射:

```
key = (adapter_id, thread_id) → session_id
```

| adapter | thread_id |
|---|---|
| feishu | `chat_id`(单聊 = open_id;群聊 = chat_id) |
| telegram | `chat_id` |
| cli | `""`(单 host = 单 thread) |

运行时:
- 入站消息到达 → adapter 查内存映射拿 session_id;查不到 → 生成新 session_id + 写回内存 + 副本写 `system.json`
- /clear → 生成新 session_id 覆盖该 key,副本写盘
- 想回到旧会话 → 编辑 `system.json` 把 key 指回旧 sid,重启 serve 生效

### 1.1 主会话上下文(main context)

- 任何用户的问题、回复**默认就是 PH**(占位符)。用户消息进入 → 立即创建 PH 占位,真正的回答 / 工具 / 思考都在后台 Run 里跑,Run 完成时**回写**该 PH。
- 主上下文**维护分叉点**。Run 回写完成后 PH 消失,被替换为完整上下文(用户消息 + tools + 思考 + 回答)。
- 主上下文写操作通过 `ContextSDK` 内部锁串行化。

### 1.2 子线程上下文(每个 Run 的 prompt)

- 子线程上下文 = **fork 那一刻主上下文的快照**。
- 子的 PH 是"先复制后写入" —— fork 时主里还没它自己的 PH,fork 之后主才追加;子永远看不见自己的 PH。
- 后来的子线程会把"前一个子线程已经留在主里的 PH"一起复制走。这些 PH 通过 §1.3 的协议展给 LLM。

### 1.3 PH 类型 + 模型可见展开

PH 有两类:

| 类型 | kind | 模型可见展开 | 用途 |
|---|---|---|---|
| 普通 PH | `regular` | **1 条 assistant 消息**(下文逐字) | 用户提问对应的 turn 占位 |
| compact PH | `compact` | **0 条**(完全隐身) | compact 运行期间的占位 |

#### 普通 PH 序列化文本(逐字)

```
assistant: 正在后台回答你的问题{ph.label}，请勿重试此请求。
```

例如 `label="review xana"`:

```
assistant: 正在后台回答你的问题review xana，请勿重试此请求。
```

#### compact PH 序列化

读模型上下文时**整条跳过**。等于"少一条消息",纯透明。

#### 序列化规则

- 通过 §1.9 `TurnMessage` 序列化器统一处理。
- **任何**内部元数据(`run_id`、`ph_run_id`、`message_id`、debug 字段等)**绝对不进 prompt**。模型看到的 PH 就是普通 1:1 聊天的一句 assistant 回复,无任何标识泄漏。

### 1.4 Run 后台运行不影响前台

- 所有 tools / 流处理在后台 Run 内部完成,事件流入该 Run 的 `RenderState`。
- 前台卡片仅同步状态(订阅当前所属 Run 的事件,patch 卡)。
- 后台 Run 与前台 Run 各自独立,照样跑、照样产生事件 —— 只是不被前台卡片渲染(其对应卡 frozen)。

### 1.5 compact 协议(compact 自己是个 PH)

compact 与普通 run 走同一套 PH `replace_placeholder` 协议,完全消除 race。

#### A. 没有任何后台运行(空闲态)
- 直接 append 一个 `Placeholder(kind="compact")` 在 main 末尾
- compact run 跑完后调 `replace_placeholder(compact_run_id, (summary_msg,))` 写回自己
- main 上多出一个 compact 摘要节点

#### B. 有后台 regular PH 在运行
- 找到 main 里**最靠前**的 regular PH 位置 K
- 在 K **前面**插入 `Placeholder(kind="compact")`(占位 0 模型可见)
- compact run 启动,以"compact PH 前面所有内容 + 那个 regular PH 自身展开"作为 prompt
- 跑完调 `replace_placeholder(compact_run_id, (summary_msg,))`,summary 取代 compact PH 位置
- 摘要内容覆盖 compact PH 前面所有节点(即"PH 点之前的内容替换为摘要")

#### 4 条强制约束

1. **隐身**:模型读上下文时 compact PH = 0 条。
2. **位置**:compact PH 永远插在"最靠前 regular PH 的前面"。
3. **读取协调**:任何新 run fork main snapshot 之前,SDK 先检查"最靠前 regular PH 再前面有 compact PH 吗?":
   - 没有 → 直接 fork
   - 有,已 replaced(已变 summary)→ 直接 fork
   - 有,还在 running → **阻塞等待**,直到 compact PH 被 replace 再 fork
4. **会话级单实例锁**:同一 session_id 任意时刻**最多一个** compact 在跑(SDK 内部 `asyncio.Lock` per sid)。代码 bug / 双触发 / 并发,都序列化。

#### 关键点

- 写回靠 `run_id`,不靠位置 —— `replace_placeholder` 按 run_id 查 PH。compact 不会动其他 PH。
- compact PH 的占位 `content` 任意(reader 看不到);replace 时写入 `MemoryMessage(role="compact", content=<summary>)`,这条要被后续 fork 看见。
- 取消:`replace_placeholder(compact_run_id, ())` 替换为空,等于撤销 PH;reader 看到等同没 compact 发生过。

### 1.6 子会话的 compact(子 Run 内部触发的 compact)

- **第二次起不再读主上下文**(已经有过自己的压缩结果,接着压自己积累的新内容)。
- **首次压缩**只看一件事:**它能看到的最靠前分叉点,它前一个消息一定是用户问题**:
  - 那条"前一个消息"已经是 compact 结果 → **直接用**,跳过自己压缩
  - 那条已经回写(不再是 PH 了)→ 看下一个最靠后的分叉点,必是自己的 PH(它没结束,不然不会由它触发 compact);由自己压缩
- 所有读写通过 SDK 串行化。

### 1.7 /clear

- = 对当前 `(adapter_id, thread_id)` **换 session_id**(`SessionResolver.rotate`)
- 旧 session 的 active_context、memory tree、bg run **全部留在内存里**,SDK 通过旧 sid 仍可访问
- 触发 detach(同 §1.8):当前前台卡 frozen
- 旧会话对应的后台 Run **不杀**,继续跑完后写回旧 sid 的内存
- 新会话起一张新卡
- 想回到旧会话:编辑 `system.json`,key 指回旧 sid,重启 serve

### 1.8 attach / detach

- attach / detach **仅对前端卡片样式**生效,对后端 Run / 上下文 / PH / 回写**零影响**。

**detach 触发方式只有两条**:
1. "后台回答"按钮点击
2. 任何 submit(任何普通消息、`/compact`、`/clear`、`/help`、`/attach`、其他菜单选项)

**/attach 流程**:
- submit `/attach`(无参)→ 此时此刻判断后台候选 Run → `build_attach_menu` 弹菜单卡
- 默认 highlight 当前 detached 的那个
- 用户点列表项 → submit `/attach <run_id>` → **新建一张 fg 卡**(新 card_id + 新 message_id)
- 即使目标 Run 已 terminal,attach 依然新建卡显示其终态。**从不复活旧卡**。
- 旧 fg 卡(如有)同时被 detach 走 frozen 路径。

### 1.9 Message 类 + 平台序列化器

所有上下文条目都是 `xana.context.message.Message`(ABC)的子类。**唯一**与模型对话的出口。任何绕开此出口的"直接拼 dict"都视为裂脑,代码层禁止(§3 ContextSDK 强制)。

#### 模块布局

```
xana/context/
    message.py      # Message ABC + public 子类(只暴露 public 字段)
    _internal.py    # frozen private dataclasses (run_id / ph_id / debug 等)
                    # 模块入口断言:只允许 sdk.py import,其他 caller 直接 ImportError
    sdk.py          # ContextSDK facade
    _store.py       # 内部 store + 锁;业务代码 import 即报错
```

#### Message ABC + public 子类

```python
# message.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xana.context._internal import _PlaceholderInternal, _ToolCallInternal


class Message(ABC):
    @abstractmethod
    def to_anthropic(self) -> list[dict]: ...   # 空列表 = filter
    @abstractmethod
    def to_openai(self)    -> list[dict]: ...


@dataclass(frozen=True)
class UserMessage(Message):
    text: str
    def to_anthropic(self): return [{"role": "user", "content": self.text}]
    def to_openai(self):    return [{"role": "user", "content": self.text}]


@dataclass(frozen=True)
class AssistantMessage(Message):
    text: str
    tool_calls: tuple["_ToolCallInternal", ...] = ()
    # anthropic: content blocks [{type:"text"...},{type:"tool_use"...}]
    # openai: {role:"assistant", content, tool_calls:[...]}
    def to_anthropic(self): ...   # 各家差异在此吸收
    def to_openai(self): ...


@dataclass(frozen=True)
class ToolResultMessage(Message):
    tool_use_id: str
    content: str
    ok: bool
    # anthropic: {role:"user", content:[{type:"tool_result", tool_use_id, content}]}
    # openai:    {role:"tool", tool_call_id, content}
    def to_anthropic(self): ...
    def to_openai(self): ...


@dataclass(frozen=True)
class RegularPlaceholder(Message):
    label: str                              # user 原问题(public)
    _internal: "_PlaceholderInternal"       # run_id / ph_id 等(private)
    def to_anthropic(self):
        return [{"role": "assistant",
                 "content": f"正在后台回答你的问题{self.label}，请勿重试此请求。"}]
    def to_openai(self):
        return [{"role": "assistant",
                 "content": f"正在后台回答你的问题{self.label}，请勿重试此请求。"}]


@dataclass(frozen=True)
class CompactPlaceholder(Message):
    _internal: "_PlaceholderInternal"
    def to_anthropic(self): return []       # 隐身
    def to_openai(self):    return []       # 隐身


@dataclass(frozen=True)
class CompactSummary(Message):
    summary: str
    _internal: "_PlaceholderInternal"
    # 模板正文住本类(单源):卡片 body / 模型可见 / main 节点 content 都引用此处
    POST_COMPACT_TEMPLATE = (
        "此会话正在从上一次因上下文不足而中断的对话中继续。"
        "以下摘要涵盖了对话的早期部分。\n\n"
        "摘要：\n{summary}\n\n"
        "如果你需要压缩前的具体细节(如确切的代码片段、错误信息或你生成的内容),"
        "请阅读完整转录记录:{transcript}\n"
        "继续对话,无需向用户提出任何进一步的问题。直接继续——不要确认摘要,"
        "不要复述正在发生的事情,不要以\"我将继续\"或类似语句开头。"
        "如同中断从未发生一样继续上一个任务。"
    )
    def _render(self) -> str:
        return self.POST_COMPACT_TEMPLATE.format(
            summary=self.summary, transcript=self._internal.transcript_path)
    def to_anthropic(self): return [{"role": "user", "content": self._render()}]
    def to_openai(self):    return [{"role": "user", "content": self._render()}]
```

#### Private 元数据(路线 3:public/private 拆两半)

```python
# _internal.py
import sys
from dataclasses import dataclass

_ALLOWED_CALLER_MODULES = {"xana.context.sdk", "xana.context._store"}
_caller = sys._getframe(1).f_globals.get("__name__", "")
if _caller not in _ALLOWED_CALLER_MODULES:
    raise ImportError(
        f"xana.context._internal is private — only ContextSDK may import. "
        f"caller={_caller!r}"
    )

@dataclass(frozen=True)
class _PlaceholderInternal:
    run_id: str
    ph_id: str
    transcript_path: str   # 给 CompactSummary 模板用
    # ...其他 debug / 路由 字段

@dataclass(frozen=True)
class _ToolCallInternal:
    call_id: str
    name: str
    arguments: dict
    # ...
```

业务代码(adapter / harness / 命令处理)拿到 `Message` 实例后,**只能**读 public 字段(`label`、`text`、`summary` 等);访问 `._internal` 字段时类型注释字符串(`TYPE_CHECKING`)阻挡静态分析,运行时 SDK 顶层把它们封装在受控 API 后(读取需调 SDK 方法,不准直接戳)。

#### Context 序列化(SDK 顶层入口)

```python
# sdk.py
class ContextSDK:
    def to_anthropic(self, sid: str, *, system_content: str) -> list[dict]:
        out = [{"role": "system", "content": system_content}]
        for m in self._store.snapshot(sid):
            out.extend(m.to_anthropic())   # 空列表自然 filter
        return out

    def to_openai(self, sid: str, *, system_content: str) -> list[dict]:
        out = [{"role": "system", "content": system_content}]
        for m in self._store.snapshot(sid):
            out.extend(m.to_openai())
        return out
```

system_content 由 harness 单独传入(从 `build_system_prompt(...)` 来),**不**走 Message 类。

#### 模板归属(铁律)

凡是"模型可见 + 卡片可见"复用的模板(如 `POST_COMPACT_TEMPLATE`):
- 模板正文住对应 Message 子类(`CompactSummary`)
- `harness_sdk/compact_prompt.py` 只放"让模型生成摘要"的指令(`BASE_COMPACT_PROMPT` / `NO_TOOLS_PREAMBLE` / `NO_TOOLS_TRAILER`)
- **单源**:
  - `CompactSummary.to_anthropic/openai()` → 给模型
  - 卡片 compact 折叠面板 body → 引用同一渲染
  - compact run 完成回写 main 的节点 content → 同一段

#### 铁律汇总

- 内部元数据(`run_id`、`ph_id`、`call_id` 等)**绝对不进** serialize 输出
- 序列化是**唯一**与模型对话的出口
- `_internal.py` 只允许 ContextSDK 模块 import,其他 caller import 即 `ImportError`
- 没有 SystemMessage —— system 由 harness 单独传 SDK 顶层 prepend

---

## 2. Run 状态机

```
                  submit (fork from main snapshot via SDK)
                       │
                       ▼
              ┌─────────────────┐
              │   streaming     │  后台跑,事件流持续
              │   (前端 fg)      │  前端卡 = Run 实时镜像
              └────────┬────────┘
                       │ 用户发任何消息 / 菜单 / /compact
                       │ → 前端按钮 = "后台回答" 同效
                       ▼
              ┌─────────────────┐
              │   streaming     │  后台继续跑(不受影响)
              │   (前端 bg)      │  前端卡 frozen,零更新
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

Run 状态(streaming → terminal)由后端事件驱动。前端 detach 不杀 Run、不影响状态机。fg/bg 只是"卡是否还订阅事件流"的差别。

---

## 3. 上下文 SDK(ContextSDK)

所有 channel(feishu / tg / cli)、所有命令处理(/clear / /compact / /attach)、harness 必须通过 `ContextSDK` 句柄操作上下文。代码层强制:

- adapter / harness / 命令 处理代码**直接字段访问 SDK 内部组件就报错**(`__slots__` + property + 调用栈检查)
- 启动时若任何上下文操作的代码路径没有 SDK 句柄注入 → **启动失败**(ImportError / AssertionError)
- 没有 fallback、没有"如果 resolver 为 None 就走 BindingStore"这类后路

SDK 内部收敛:
- ActiveContextStore(主+子上下文 + PH + replace_placeholder + 串行锁)
- SessionResolver((adapter, thread) → sid 映射 + 副本写)
- MainWriter / HistoryMirror(磁盘副本)
- Compact 协调(§1.5 的位置查找、读取阻塞、sid 锁)
- TurnMessage 序列化器(§1.9)

唯一对外接口集合:
- `resolve(adapter_id, thread_id) -> sid` / `rotate(...)` (会话身份)
- `snapshot_for_fork(sid, agent) -> tuple[TurnMessage|PH, ...]`(自动处理 compact PH 阻塞)
- `append_user(sid, agent, text) -> None`
- `append_placeholder(sid, agent, ph) -> None`
- `replace_placeholder(run_id, messages: tuple[TurnMessage, ...]) -> None`
- `serialize_for_openai(turn_messages) / serialize_for_anthropic(...)`

---

## 4. 卡片(MVVM):一个模板,多个 View

### 4.1 模板定义

只维护一个"卡片模板"。它定义所有可能元素(section)、各元素状态(streaming / detached / terminal / cancelled / completed)、元素**先后顺序**。各 View 根据"有没有相应元素 / 处于哪些状态"显示。

### 4.2 Section 顺序(固定)

```
1. 压缩进度条(一行文字 + ▰…▱)
2. 压缩下拉                       ┐
3. 思考过程下拉                    │  panel 段
4. 工具下拉(默认 3 个)+ 展开按钮 │
5. 回答块(主答案)                ┘
6. 脚标(footer)
7. 顶部按钮(取消回答 / 后台回答 / 取消)
```

button row 视觉位置由各 View 决定(飞书 top 在最顶,TG 多用 bottom inline_keyboard)。

compact 摘要排在 answer 前的原因:语义上 compact 是 PH 点之前的摘要,回答出现在 PH 之后 → UI 顺序"摘要在上,回答在下"。

### 4.3 MVVM 分层

| 层 | 提供方 | 职责 |
|---|---|---|
| Model | `UnifiedCardState` 等 dataclass | section 字段 + 状态 enum + 小工具 |
| ViewModel | `unified_card_events.apply_event` | 后端 `HarnessEvent/AgentEvent/ProgressEvent` → Model。**唯一状态推进入口** |
| View | `unified_card_feishu.render` / `_telegram.render` / `_cli.render` | UnifiedCardState → 平台 schema |
| Agent 接口 | `xana.harness_sdk` 的事件流 | Agent 不见 Card,只发事件 |
| Adapter | `FeishuAdapter` / `TelegramAdapter` / `CliAdapter` | 维护 card_id↔message_id↔run_id 绑定;调 apply_event;调 renderer;决定 send vs patch;按钮路由 |

---

## 5. 卡片元素详细规格(以飞书为基准,差异点 §7 单列)

### 5.1 压缩进度条

- 10 段(▰/▱)
- 7 段输入阶段:3s 进一格(估算)
- 模型开始输出 → 直接跳到 7
- 3 段输出阶段:按 compact 输出 N 个 section 进度算

### 5.2 压缩下拉(折叠面板)

- streaming:title "压缩中",body 已积累的摘要片段
- completed:title "已压缩 N 条消息",body 完整摘要
- cancelled:title "压缩已取消"

### 5.3 思考过程下拉

- 默认折叠
- 标题:有内容 → "❋ 思考过程";空 → "❋ 思考中"
- body = 模型 reasoning 流

### 5.4 工具下拉

- 默认 3 个 visible(`TOOLS_VISIBLE_LIMIT = 3`)
- 每个 tool 是一个折叠面板
- header 文本:
  - **bash**: 显示 LLM 输出的短标题(prompt 让模型给 `description` 字段)
  - **其余工具**: 显示参数,优先级固定 `description > path / file_path / file / cmd / command / url / query / pattern > 第一个非空 kv`
  - 完整头格式:`✎ {name} · {summary} · {elapsed} · {status}`(status ∈ ok/fail/cancel)
- body 渲染:
  - 飞书:截 800 字符(单卡 30KB 上限,避 200800)
  - TG:**top 3 行 + 每行最多 60 字**
- 超出 3 个:
  - 飞书:"展开 ✎×N" **按钮**(N = `tools_pending = len(_pending_tools_buf)`)
  - TG:"展开 ✎×N" **文字提示**(非按钮)
  - 触发后:`load_pending_tools`,buf → tools,pending=0
  - 之后新 tool 又进 buf → 按钮/提示重新出现

### 5.5 回答块

- fg 默认展开;bg 默认折叠
- 标题:有内容 → "✦ 回答";空 → "✦ 回答中";cancelled → "✦ 回答已取消"
- 内容渲染:**只有回答用 markdown**(其他段落纯文本)
- streaming 截断守门:`*` / `_` / `` ` `` / ``` 等末尾补闭或截到安全 boundary。TG 走 telegramify-markdown;飞书自身容错足够,不补。

### 5.6 脚标(footer)

| Run / Card 状态 | 脚标 |
|---|---|
| streaming(fg) | `🟡 model · 上下文 Nk/Mk · 回答用时 53s` |
| terminal(完成) | `🟢 model · 上下文 Nk/Mk · 回答用时 53s` |
| cancelled | `🔴 model · 上下文 Nk/Mk · 回答用时 53s` |
| bg(frozen) | `🟡 正在后台回答` |

**互斥规则**:
- "🟡 正在后台回答" 与三件套(model + ctx + 回答用时)互斥。bg_frozen 态下绝不出现三件套。
- 三件套**绑定**:任一字段缺失,整段不渲染。
- 菜单段(如 `compact 用时 10s`)永远排最尾;bg_frozen 也允许尾巴挂菜单段。

### 5.7 顶部按钮

| Run 状态 | 按钮 |
|---|---|
| streaming(fg) | "取消回答" + "后台回答" |
| streaming(bg,仍未完成) | "取消回答"(允许取消后台 run) |
| compact 单独运行(answer 不活跃) | "取消"(`/compact_cancel`) |
| terminal / cancelled | 无 |

- 点"后台回答" → 当前前台卡 detach
- 任何 submit 也等价 detach
- 按钮 callback 以 run_id 编号

---

## 6. bg(frozen)状态的精确语义

- 卡片不再动(不更新内容、tool panel、按钮样式)
- 直到 Run terminal → `ph_replaced`
- 一次性刷新成"流最后一刻"(完整 RenderState 一次性 patch)
- 期间后台 Run 的所有事件累积在 Run 自己的 RenderState,不写卡(adapter `_deliver` 由 `is_frozen` 门挡)

### TG/飞书 bg 差异

- 飞书:tools_pending 提示是**按钮**,detached 态下渲染成静态文字"_后台 ✎ 中_"不可点
- TG:tools_pending 提示本来就是文字,detach 前后无变

---

## 7. TG / 飞书 / CLI 渲染差异表

| 项 | 飞书 | TG | CLI |
|---|---|---|---|
| 折叠 panel | `collapsible_panel` 灰底 + chevron | expandable blockquote(`**>` + `\|\|`) | Textual Collapsible 节点 |
| 工具 body | 截 800 字符 | **top 3 行 + 每行 60 字** | 截 4096 字符 |
| tools_pending 提示 | 按钮 | **文字**(非按钮) | 文字 |
| 超长 thinking / answer / tools | 直接折叠展开看全 | 底部 3 个按钮("展开 思考 / 工具 / 答案" 全量)→ 全屏单段卡,翻页 | 滚动条 |
| 翻页粒度 | 无 | tools 一页最多 4 个;answer 按 TG 单消息长度切页 | 无 |
| markdown 渲染 | 只回答用 markdown | 只回答用 markdown;telegramify-markdown 容错 | 纯文本(无 markdown 解析) |
| 命令 linkify | 无 | `linkify_commands=True` 时正文 `/cmd` 原生可点 | 无 |

CLI 详细见 [channels/cli.md](./channels/cli.md)。

---

## 8. 端到端事件流(权威流程)

### 8.1 首次 submit("你好")
1. SDK `append_user(sid, "chat", "你好")` → main 追加
2. SDK `snapshot_for_fork(sid, "chat")` → 拿到当时快照(R1 看不见自己的 PH)
3. SDK 创建 Run R1,fork 完后 `append_placeholder(sid, "chat", Placeholder(R1, kind="regular", label="你好"))` 追加 PH
4. 新建 Card C1,首次 send → 拿 message_id
5. R1 后台跑,事件流 → `apply_event(C1)` → patch C1
6. R1 完成 → SDK `replace_placeholder(R1, (assistant_msgs, tool_msgs, ...))` → main 上 PH 变真节点
7. `ph_replaced` → `apply_ph_replaced(C1)` → terminal

### 8.2 streaming 中再发("再问一条")
1. 前端 detach C1:patch 一次 → frozen,**后端 R1 不受影响**
2. SDK `append_user(...)` 追加用户消息
3. SDK `snapshot_for_fork(...)` 拿快照(看见 R1 PH,按 §1.3 展为 1 条 assistant)
4. 创建 R2,append R2 PH
5. **新建 Card C2** → R2 streaming patch C2
6. R1 完成 → `replace_placeholder(R1, ...)` → `ph_replaced` 找到 C1 → patch 终态,`terminal_from_bg=True`
7. R2 完成 → patch C2 终态

### 8.3 /compact 中途介入
1. R1 正在 streaming,C1 是 fg
2. 用户 submit `/compact`
3. 前端 detach C1
4. SDK 拿 sid 级 compact 锁
5. SDK 查 main 最靠前 regular PH 位置 K(= R1 PH 索引)
6. SDK 在 K 前面插入 `Placeholder(kind="compact", run_id=Rc, ...)`
7. 创建 compact Run Rc(没有 user 节点,prompt = compact PH 前面所有内容 + R1 PH 展开)
8. 找到 C1(R1 对应)→ `apply_event(compact_started)` → C1 多 compact 段,footer 加"compact 用时 t"
9. compact 进度:input(7 格,~3s/格,模型开输出跳 7)→ streaming(7→10,按 sections)
10. Rc 完成 → SDK `replace_placeholder(Rc, (MemoryMessage(role="compact", content=summary),))` → main 上 compact PH 位置变 summary,**compact PH 前面所有节点被替换覆盖**
11. `finalize_compact` → C1 "已压缩 N 条消息";SDK 释放 sid 锁
12. R1 继续跑;完成时 `ph_replaced(R1)` → C1 answer 段 patch 终态;**C1 同时承载 compact 段 + answer 终态**
13. 用户下一条消息走 8.2 → 新建 C2

读取协调:Rc 跑的时候,任何新 run 走 `snapshot_for_fork` 会**阻塞等待**到 Rc 替换完(§1.5 约束 3)。

### 8.4 /attach
1. submit `/attach`(无参)→ SDK 此时此刻列后台候选 Run → `build_attach_menu`
2. 默认 highlight 当前 detached 的 Run
3. 用户点项 → submit `/attach <run_id>`
4. 当前 fg(如有)→ detach
5. **新建 Card Cn** 绑定目标 Run
6. Cn 接收该 Run 事件;旧 card 保持 frozen
7. 即使目标 Run 已 terminal,attach 依然新建卡显示终态
8. 后端 Run 状态不动

### 8.5 /cancel
- top button "取消回答" → submit `/cancel <run_id>`
- 后端真取消 Run;Card 走 cancelled 形态(`cancel_answer`,footer 🔴)
- 后续 delta no-op

### 8.6 /clear
- 触发 detach → 当前 fg 卡 frozen
- SDK `rotate("feishu/tg/cli", thread_id)` 生成新 sid + 副本写 system.json
- 旧 session 内存 + 后台 Run **不动**,继续在旧 sid 下回写
- 新 session 起一张新卡

---

## 9. 不变量(merge-gate 断言)

| # | 不变量 | 关联 |
|---|---|---|
| I1 | 1 Run ↔ 1 PH ↔ 1 Card 终生绑定 | §1.1, §2 |
| I2 | Card 一旦 send,后续都 patch 同 message_id,不重发覆盖 | §4.3 |
| I3 | 前端 detach / attach 不改变后端 Run 状态、不杀 Run、不影响 PH | §1.8 |
| I4 | submit 任何消息(含 /compact)→ 当前前台卡立即 detach | §5.7 / §8.2 |
| I5 | /compact 段挂在"最早未完成 PH 所属的 Card"上,不新建卡(除非空闲态) | §1.5 / §8.3 |
| I6 | /attach 总是新建卡 | §1.8 / §8.4 |
| I7 | bg(frozen)Card 从 detach 到 ph_replaced 之间零更新;ph_replaced 一次性 patch 终态 | §6 |
| I8 | tools_pending 只数未展开 tools(buf 长度);load_pending_tools 后归零 | §5.4 |
| I9 | footer 三件套(model + ctx + 回答用时)绑定;缺一不渲染 | §5.6 |
| I10 | bg_frozen 脚标永不出现三件套(互斥) | §5.6 |
| I11 | 任何 channel View 对同一 UnifiedCardState 输出语义一致 | §4.3 |
| I12 | 子会话 compact:首次查"同分叉点已压缩可用就用";二次起不再读主上下文 | §1.6 |
| I13 | session_id 来源于统一映射 `(adapter_id, thread_id) → session_id`;禁止任何 channel 走 "default" 路径 | §1.0 |
| I14 | thread_id 取值固定:feishu/tg = chat_id;cli = `""`;同 thread 在同 channel 只对应一个 active sid | §1.0 |
| I15 | 内存是 session_id 映射的唯一 source of truth;`system.json` / `history/` 是副本写 | §1.0 |
| I16 | `system.json` 只在启动时读一次;运行中不再读;手编需重启生效 | §1.0 |
| I17 | /clear = 对当前 `(adapter_id, thread_id)` 换 session_id(不清旧内存);旧 session 全留底 | §1.7 |
| I18 | 序列化给模型的消息中**禁止**出现 run_id / UUID / 内部字段 | §1.3 / §1.9 |
| I19 | 普通 PH 序列化必须**逐字**为 1 条 assistant `"正在后台回答你的问题{label}，请勿重试此请求。"`;compact PH 序列化为 **0 条** | §1.3 |
| I20 | compact 是 PH(`kind="compact"`),跑完通过 `replace_placeholder(compact_run_id, ...)` 写回,不调 `replace_range` | §1.5 |
| I21 | 同 session_id 任意时刻最多一个 compact 在跑(SDK sid 锁) | §1.5 |
| I22 | 新 run `snapshot_for_fork` 检测到 running compact PH 时**阻塞等待**到其被 replace | §1.5 |
| I23 | 所有上下文操作必须通过 `ContextSDK` 句柄;adapter / harness / 命令处理直接访问 SDK 内部组件 = 启动失败 | §0 / §3 |

---

## 10. 测试合约(committed,禁止糊弄)

`tests/e2e_real/test_card_state_contract.py`:
- `pytest -m merge_gate` 触发,默认 skip
- 失败 = 行为偏离本文 → **必须修代码**;不得借口"别人引入"
- 修改本测试 = 修改契约 → 先改本文 + 用户确认

---

## 11. Channel View 详细规格

具体渲染细节见:
- 飞书 → [channels/feishu.md](./channels/feishu.md)
- TG → [channels/telegram.md](./channels/telegram.md)
- CLI → [channels/cli.md](./channels/cli.md)
