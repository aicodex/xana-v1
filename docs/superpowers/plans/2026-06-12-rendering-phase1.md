# xana 消息渲染对齐 yuxu_backup(Phase 1)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** tg/feishu 消息渲染对齐 yuxu_backup:每轮新气泡、thinking/工具折叠块(xanac 同款标题、全量 args/结果)、模型选项按钮 + 点击回流、token/上下文预算脚标(967k 占位)。

> Phase 2(compact, spec: `todo_ai/compact.md`)与 Phase 3(prompt 对齐 CC, spec: `prompt-fix.md`)**等本阶段完成后再单独写计划**——代码会变,提前写会失准。

**Architecture:** Phase 1 在 adapter 层加 per-turn `TurnDraft` 状态机(平台无关数据 + 平台渲染器),消费 `AgentEvent.harness_event.kind`(model_delta/tool_started/tool_finished/final)——不改任何锁定 contract(`AgentEvent.payload` 是自由 dict)。thinking 通过 provider 在 `text_delta` payload 加 `channel="thinking"` 传递。Phase 2 按 todo_ai/compact.md 在 DefaultHarness 内联阈值检查 + 同模型摘要。Phase 3 新建 `harness_sdk/system_prompt.py` 静态常量 + `build_system_prompt()` 动态拼装。

**Tech Stack:** Python 3.11+, lark-oapi(已接 WS), httpx Telegram Bot API(HTML parse mode), pytest。

**参考实现(只读,不可整文件复制,按机制移植):**
- yuxu 渲染: `/home/xzp/project/yuxu_backup/src/yuxu/services/gateway/draft.py`(DraftMessage/tool_block_head_line/coalescing)
- yuxu feishu 卡片: `/home/xzp/project/yuxu_backup/src/yuxu/services/gateway/adapters/feishu.py:449-672`(`_build_feishu_card`/`_thinking_collapsible_panel`/`_tool_collapsible_panel`/`_draft_buttons_to_feishu`)
- yuxu telegram: `/home/xzp/project/yuxu_backup/src/yuxu/services/gateway/adapters/telegram.py:226-400`(finalize 拆分)、`:560-660`(`_draft_buttons_to_telegram`/`_md_to_telegram_html`)、`:481-560`(callback_query)
- yuxu 按钮提取: `/home/xzp/project/yuxu_backup/src/yuxu/agents/chat_agent/handler.py:310-350`(`_BUTTONS_RE`/`_extract_buttons`)
- compact spec(prompt 全文在内): `todo_ai/compact.md`
- prompt spec: `/home/xzp/project/best_agent/ds_review/xana_review/prompt-fix.md`;中文翻译全文: `/home/xzp/project/best_agent/ds_review/ROADMAP/AGENT_LOOP_PROMPT.md`;拼装顺序: `.../ROADMAP/PROMPT_ASSEMBLY_ORDER.md`;CC 原文: `/home/xzp/project/best_agent/claude-code-2.1.88-src/constants/prompts.ts` 与 `services/compact/prompt.ts`

---

## 硬约束(每个任务开工前必读)

1. **锁定文件**:不得修改任何既有 `interfaces.py` / `contracts.py`。本阶段没有例外。`AgentEventKind`/`HarnessEventKind`/`ModelEventKind` 都**不加新值**——thinking 走 payload `channel` 字段,工具事件已有 `tool_started`/`tool_finished`。
2. 本地提交,**绝不 push**。
3. 已知预存在失败、忽略:`test_xanac_empty_input_exits_nonzero`。
4. 测试从仓库根 `/home/xzp/project/xana` 跑 `python -m pytest`。
5. **git stash 里有飞书机器人误写的半成品 compact 实现**(`feishu-bot unintended auto-compact edits`)——不要 pop、不要参考,按本计划全新实现。
6. 改完代码验证用 `~/.xana/bin/xana sync`(会自动重启 serve)——但这属于人工冒烟,任务内只跑 pytest。

## 已锁定的设计决策(不要重新论证)

- **每轮新气泡(已拍板修订)**:adapter 维护 `session 状态.turns: dict[run_id → TurnState(draft, message_id, coalescer, tool_msg_ids, started_ts)]`,事件按自己的 run_id 路由到对应 TurnState——交错/晚到的旧 run 事件更新自己的气泡,不与新轮互相覆盖(为将来后台轮铺路)。turns dict 按序保留最近 4 轮即可(老的 pop)。无 harness_event 的事件路由到最近一轮。
- **流式+最终不重复(yuxu skip_flush 教训, handler.py:4525)**:final 事件路径 = 取消 coalescer 尾随 → **唯一一次** finalize 渲染。绝不在 final 处理里先普通 flush 再 finalize;telegram 拆分分支里 answer 只 send/edit 一次。这是验收点:测试必须断言 final 后总消息数(气泡数)正确、无重复 answer。
- **工具标题用 xanac 风格**(不是 yuxu 的 `🔧 name · args`):`● ToolName(preview)`,失败追加 ` — FAILED`,运行中追加 ` — ⏳ 执行中`,完成可追加 ` — {N}ms`。preview 复用 xanac 的 `_tool_preview`(description 优先、Bash 注释、主字段)。glyph 用 `●`(feishu plain_text 标题里 emoji 安全)。
- **折叠块内容与 xanac 不同——是全量**:展开体 = 完整请求参数(```json 代码块)+ 完整 render 结果(`tool_finished` payload 的 text)。各自截断到 3000 字符(尾部加 `\n…(truncated)`)。
- **thinking 默认折叠**(流式中也折叠,yuxu 注释:用户偏好);回答不折叠;feishu 用 `collapsible_panel`(**icon 字段必填,缺了飞书静默丢弃整个面板**——yuxu feishu.py:649 验证过),telegram 用 `<blockquote expandable>`。
- **telegram 工具块 = 独立 sealed 消息**(yuxu append_tool_block 模式:tool_started 先发 ⏳ 占位消息,tool_finished 按 call_id 原地 edit 成结果);**feishu 工具块 = 同一张卡里的折叠面板**(卡片 PATCH 天然支持)。
- **finalize 拆分(telegram)**:有 thinking 时,流式消息 edit 成 thinking 气泡,answer+footer+按钮作为新消息;无 thinking 时流式消息原地 edit 成 answer(yuxu telegram.py:313-395 的两分支,含"空殻清理"分支)。
- **按钮**:模型输出末尾 `[选项: a | b | c]`(`_BUTTONS_RE`,容忍缺右括号、全角冒号/竖线),提取后从正文删除,一行一个按钮,最多 6 个。点击回流 = 把按钮文本当用户消息重新进 gateway。feishu 走 card.action.trigger(WS 客户端需恢复 card 回调注册),telegram 走 callback_query(callback_data 放索引,因 64 字节限制,真实文本存 adapter 侧 map)。
- **脚标(两端 + xanac 一致化)**:`model: {m} | tokens: {in} in / {out} out | context: {used}/{967k} ({pct}%)`,context used = 本轮最后一次 usage 的 `exact_input_tokens`(≈下次请求的上下文占用)。`CONTEXT_BUDGET_TOKENS = 967_000` 常量放共享模块,注释 `TODO(compact): Phase 2 接 ModelConfig.context_window`。Phase 2 完成后脚标读真实值。
- **thinking 传输**:provider(anthropic_deepseek)解析 DeepSeek 的 `reasoning_content` 增量 → 发 `ModelEvent(kind="text_delta", payload={"text": ..., "channel": "thinking"})`。DefaultHarness 的 model_delta 已 `payload=dict(event.payload)` 透传;**累计 final 文本必须排除 channel=="thinking" 的增量**(查 default.py 里 cumulative 文本的拼接处,加 channel 过滤)。

## File Map

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/xana/agent_adapter/_tool_titles.py` | 创建 | xanac 工具标题逻辑共享化(_tool_preview 等) |
| `src/xana/agent_adapter/cli.py` | 修改 | 改为 import 共享模块 |
| `src/xana/agent_adapter/channels/_draft.py` | 创建 | TurnDraft + tool_block_head_line + _extract_buttons + footer 构建 |
| `src/xana/llm_driver/providers/anthropic_deepseek.py` | 修改 | reasoning_content → thinking channel |
| `src/xana/harness_sdk/default.py` | 修改 | final 文本排除 thinking |
| `src/xana/agent_adapter/channels/feishu_card.py` | 创建 | 卡片构建(折叠面板/按钮/脚标) |
| `src/xana/agent_adapter/channels/feishu.py` | 修改 | TurnDraft 状态机、新气泡、card action 入站 |
| `src/xana/agent_adapter/channels/feishu_ws.py` | 修改 | 恢复 card.action.trigger 注册 |
| `src/xana/agent_adapter/channels/telegram_html.py` | 创建 | md→HTML、blockquote、4KB 截断 |
| `src/xana/agent_adapter/channels/telegram.py` | 修改 | TurnDraft 状态机、sealed 工具消息、finalize 拆分、callback_query |
| tests | 创建 | 见各任务 |

---

# Phase 1 — 消息渲染对齐 yuxu_backup

### Task 1: 共享工具标题模块

**Files:**
- Create: `src/xana/agent_adapter/_tool_titles.py`
- Modify: `src/xana/agent_adapter/cli.py`(删除被移走的函数,改 import)
- Test: `tests/test_tool_titles.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_tool_titles.py"""
from xana.agent_adapter._tool_titles import (
    bash_comment_label,
    first_line_preview,
    tool_header,
    tool_preview,
    truncate,
)


def test_tool_preview_description_wins():
    assert tool_preview({"description": "List files", "command": "ls"}, "Bash") == "List files"


def test_tool_preview_bash_comment():
    assert tool_preview({"command": "# count\nwc -l x"}, "Bash") == "count"


def test_tool_header_plain():
    assert tool_header("Read", {"file_path": "/a/b.py"}) == "● Read(/a/b.py)"


def test_tool_header_failed():
    assert tool_header("Bash", {}, ok=False) == "● Bash — FAILED"


def test_tool_header_running():
    assert tool_header("Bash", {"command": "ls"}, running=True) == "● Bash(ls) — ⏳ 执行中"


def test_tool_header_elapsed():
    assert tool_header("Bash", {"command": "ls"}, elapsed_ms=42) == "● Bash(ls) — 42ms"


def test_first_line_and_truncate():
    assert first_line_preview("\n  hello world \nrest") == "hello world"
    assert truncate("a" * 100, 10).endswith("…")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_tool_titles.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 实现**

读 `src/xana/agent_adapter/cli.py` 的 `_tool_preview` / `_bash_comment_label` / `_first_line_preview` / `_truncate` / `_PREVIEW_LIMIT` / `_RESULT_PREVIEW_LIMIT` / `_ELLIPSIS`(约 210-280 行),**原样搬**到新模块 `src/xana/agent_adapter/_tool_titles.py`,去掉前导下划线导出(`tool_preview`/`bash_comment_label`/`first_line_preview`/`truncate`),并新增:

```python
TOOL_GLYPH = "●"


def tool_header(
    tool_name: str,
    tool_input: dict,
    *,
    ok: bool = True,
    running: bool = False,
    elapsed_ms: int | None = None,
) -> str:
    """xanac 风格工具标题: `● Tool(preview)`,失败/运行中/耗时追加后缀。
    tg/feishu 折叠块标题与 xanac CLI 共用此格式。"""
    header = TOOL_GLYPH + " " + tool_name
    preview = tool_preview(tool_input or {}, tool_name)
    if preview:
        header += f"({preview})"
    if not ok:
        header += " — FAILED"
    elif running:
        header += " — ⏳ 执行中"
    elif elapsed_ms is not None and elapsed_ms >= 0:
        header += f" — {int(elapsed_ms)}ms"
    return header
```

cli.py 改成 `from xana.agent_adapter._tool_titles import ...` 并删除本地副本;`_render_tool_event` 里 header 拼接改调 `tool_header(tool_name, event.payload.get("input") or {}, ok=ok)`(行为不变:CLI 不传 running/elapsed)。

- [ ] **Step 4: 跑测试 + CLI 回归**

Run: `python -m pytest tests/test_tool_titles.py tests/ -q -k "cli or xanac or tool_titles"`
Expected: 全 PASS(除已知 xanac 预存在失败)

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/_tool_titles.py src/xana/agent_adapter/cli.py tests/test_tool_titles.py
git commit -m "refactor(adapter): share xanac tool-title logic for channel renderers"
```

---

### Task 2: TurnDraft + 按钮提取 + 脚标构建(平台无关)

**Files:**
- Create: `src/xana/agent_adapter/channels/_draft.py`
- Test: `tests/test_turn_draft.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_turn_draft.py"""
from xana.agent_adapter.channels._draft import (
    CONTEXT_BUDGET_TOKENS,
    ToolBlock,
    TurnDraft,
    build_footer_meta,
    extract_buttons,
)


def test_extract_buttons_basic():
    cleaned, btns = extract_buttons("回答正文\n[选项: 是 | 否 | 再想想]")
    assert cleaned == "回答正文"
    assert btns == [[{"text": "是", "send": "是"}], [{"text": "否", "send": "否"}],
                    [{"text": "再想想", "send": "再想想"}]]


def test_extract_buttons_missing_bracket_and_fullwidth():
    cleaned, btns = extract_buttons("正文\n[选项：甲 ｜ 乙")
    assert cleaned == "正文"
    assert [b[0]["text"] for b in btns] == ["甲", "乙"]


def test_extract_buttons_none():
    cleaned, btns = extract_buttons("没有选项的正文")
    assert cleaned == "没有选项的正文" and btns == []


def test_extract_buttons_caps_at_six():
    line = "[options: " + " | ".join(f"o{i}" for i in range(9)) + "]"
    _, btns = extract_buttons("x\n" + line)
    assert len(btns) == 6


def test_turn_draft_tool_block_lifecycle():
    d = TurnDraft(run_id="r1")
    d.start_tool(call_id="c1", name="Bash", args={"command": "ls"})
    assert d.tool_blocks[0].running is True
    d.finish_tool(call_id="c1", ok=True, result="a.txt", elapsed_ms=12)
    blk = d.tool_blocks[0]
    assert blk.running is False and blk.ok is True and blk.result == "a.txt"
    d.finish_tool(call_id="missing", ok=False, result="", elapsed_ms=0)  # 容错: 不抛
    assert len(d.tool_blocks) == 2  # 未匹配的 finish 落成独立块(不丢事件)


def test_build_footer_meta_with_budget():
    class U:  # 鸭子类型 UsageSnapshot
        model = "ds-v4"
        exact_input_tokens = 96_700
        exact_output_tokens = 200
    pairs = build_footer_meta(U())
    d = dict(pairs)
    assert d["model"] == "ds-v4"
    assert d["tokens"] == "96700 in / 200 out"
    assert d["context"] == f"96.7k/{CONTEXT_BUDGET_TOKENS // 1000}k (10%)"


def test_build_footer_meta_no_usage():
    assert build_footer_meta(None) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_turn_draft.py -v` → FAIL ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
"""src/xana/agent_adapter/channels/_draft.py

每轮(run)一份 TurnDraft:平台无关的渲染状态。adapter 在事件流上累积它,
平台渲染器(feishu_card / telegram_html)把它画成原生形态。
移植自 yuxu_backup services/gateway/draft.py 的 DraftMessage,工具标题
改用 xanac 风格(_tool_titles.tool_header)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from xana.agent_adapter._tool_titles import tool_header

# TODO(compact): Phase 2 接 ModelConfig.context_window 后由配置驱动
CONTEXT_BUDGET_TOKENS = 967_000

_BODY_LIMIT = 3000  # 折叠块内 args/result 各自的截断上限


@dataclass
class ToolBlock:
    call_id: str
    name: str
    args: dict
    running: bool = True
    ok: bool = True
    result: str = ""
    elapsed_ms: int | None = None

    def header(self) -> str:
        return tool_header(
            self.name, self.args, ok=self.ok,
            running=self.running, elapsed_ms=self.elapsed_ms,
        )


@dataclass
class TurnDraft:
    run_id: str
    content: str = ""
    thinking: str = ""
    footer_meta: list[tuple[str, str]] = field(default_factory=list)
    buttons: list[list[dict]] = field(default_factory=list)
    tool_blocks: list[ToolBlock] = field(default_factory=list)

    def append_content(self, chunk: str) -> None:
        self.content += chunk

    def append_thinking(self, chunk: str) -> None:
        self.thinking += chunk

    def start_tool(self, *, call_id: str, name: str, args: dict) -> None:
        self.tool_blocks.append(ToolBlock(call_id=call_id, name=name, args=dict(args or {})))

    def finish_tool(self, *, call_id: str, ok: bool, result: str, elapsed_ms: int | None) -> None:
        for blk in self.tool_blocks:
            if blk.call_id == call_id and blk.running:
                blk.running = False
                blk.ok = ok
                blk.result = result
                blk.elapsed_ms = elapsed_ms
                return
        # 没有匹配的 started(容错): 落成独立完成块,事件不丢
        self.tool_blocks.append(ToolBlock(
            call_id=call_id, name="?", args={}, running=False,
            ok=ok, result=result, elapsed_ms=elapsed_ms,
        ))


def clip_body(s: str, limit: int = _BODY_LIMIT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…(truncated)"


# 移植 yuxu_backup handler.py:322-326 的 _BUTTONS_RE(容忍缺右括号/全角符号)
_BUTTONS_RE = re.compile(
    r"\[(?:选项|options|buttons|按钮)\s*[:：]?\s*([^\]\n]+?)\]?\s*$",
    re.MULTILINE,
)


def extract_buttons(content: str) -> tuple[str, list[list[dict]]]:
    """从 LLM 回复末尾抽 `[选项: a | b | c]` 行 → (清理后正文, 按钮行)。
    一行一个按钮,最多 6 个(yuxu handler.py:328-350 语义)。"""
    if not content:
        return content, []
    m = _BUTTONS_RE.search(content)
    if not m:
        return content, []
    options = [o.strip() for o in re.split(r"[|｜]", m.group(1)) if o.strip()][:6]
    if not options:
        return content, []
    cleaned = (content[: m.start()].rstrip()
               + ("\n" + content[m.end():].lstrip() if content[m.end():].strip() else ""))
    return cleaned.strip(), [[{"text": o, "send": o}] for o in options]


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k")


def build_footer_meta(usage) -> list[tuple[str, str]]:
    """脚标: model | tokens in/out | context used/budget (pct%)。
    usage 为 UsageSnapshot 或 None。"""
    if usage is None:
        return []
    pairs: list[tuple[str, str]] = []
    if getattr(usage, "model", None):
        pairs.append(("model", str(usage.model)))
    tin = getattr(usage, "exact_input_tokens", None)
    tout = getattr(usage, "exact_output_tokens", None)
    if tin is not None or tout is not None:
        pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
    if tin:
        pct = round(tin * 100 / CONTEXT_BUDGET_TOKENS)
        pairs.append(("context", f"{_fmt_k(tin)}/{CONTEXT_BUDGET_TOKENS // 1000}k ({pct}%)"))
    return pairs
```

- [ ] **Step 4: 跑测试** → 7 PASS
- [ ] **Step 5: Commit** `feat(adapter): TurnDraft per-run render state + button extraction + budget footer`

```bash
git add src/xana/agent_adapter/channels/_draft.py tests/test_turn_draft.py
git commit -m "feat(adapter): TurnDraft per-run render state + button extraction + budget footer"
```

---

### Task 3: provider thinking channel + final 文本排除 thinking

**Files:**
- Modify: `src/xana/llm_driver/providers/anthropic_deepseek.py`(解析 `reasoning_content`)
- Modify: `src/xana/harness_sdk/default.py`(累计 final 文本时跳过 thinking 增量)
- Test: `tests/test_thinking_channel.py`

- [ ] **Step 1: 先读两份文件**。在 anthropic_deepseek.py 找 SSE delta 解析处(现在只取 `content` 文本发 `text_delta`);DeepSeek 兼容 OpenAI 格式,thinking 在 `choices[0].delta.reasoning_content`。在 default.py 找 model_delta 的产生处(~419 行 `yield HarnessEvent(kind="model_delta", ...)`)和 cumulative 文本拼接处(final_text 来源)。

- [ ] **Step 2: 写失败测试**

```python
"""tests/test_thinking_channel.py

provider 层: reasoning_content 增量 → ModelEvent(text_delta, payload.channel="thinking")。
harness 层: channel=thinking 的增量不进 final 文本,但照常作为 model_delta 事件流出。
"""
import asyncio


def test_provider_emits_thinking_channel():
    # 用 provider 的内部 SSE-chunk → ModelEvent 转换函数直接测
    # (读 anthropic_deepseek.py 找到该转换点;若解析内联在 stream 循环里,
    #  先抽一个纯函数 _delta_to_events(delta: dict) -> list[ModelEvent] 再测)
    from xana.llm_driver.providers.anthropic_deepseek import _delta_to_events
    evs = _delta_to_events({"reasoning_content": "思考…"})
    assert evs and evs[0].kind == "text_delta"
    assert evs[0].payload["text"] == "思考…"
    assert evs[0].payload["channel"] == "thinking"
    evs2 = _delta_to_events({"content": "答案"})
    assert evs2[0].payload["text"] == "答案"
    assert evs2[0].payload.get("channel") is None


def test_harness_final_excludes_thinking():
    # 复用 tests/test_default_harness.py 的 fakes: 脚本一轮含
    # thinking 增量("T1","T2", payload channel=thinking)和正文增量("A")
    # 的模型回合(看 ScriptedTurn/FakeModelClient 怎么造 text_delta,
    # 给它加 channel 透传支持——fake 在 tests 里,可改)。
    # 断言: final 事件 payload["text"] == "A"(不含 T1/T2);
    #       model_delta 事件里能看到 channel=="thinking" 的增量。
    ...
```

(实现者:第二个测试体按 test_default_harness.py 的实际 fake 形状补全,断言语义以 docstring 为准。)

- [ ] **Step 3: 实现**
  - provider:把 delta 解析抽成模块级纯函数 `_delta_to_events(delta: dict) -> list[ModelEvent]`:`reasoning_content` 非空 → `ModelEvent(kind="text_delta", payload={"text": rc, "channel": "thinking"})`;`content` 非空 → 普通 text_delta(payload 不带 channel)。两者可同时出现(各发一个事件)。stream 循环改调它。
  - default.py:找到把 text_delta 文本累进 final 的列表(如 `cumulative_text.append(...)`),加 `if event.payload.get("channel") == "thinking": ... continue 累计`——**model_delta HarnessEvent 照常 yield(payload 透传,含 channel),只是不进 final 文本**。

- [ ] **Step 4: 跑** `python -m pytest tests/test_thinking_channel.py tests/test_default_harness.py -v` → 全 PASS
- [ ] **Step 5: Commit** `feat(llm): DeepSeek reasoning_content → thinking channel deltas, excluded from final text`

---

### Task 4: feishu 卡片构建器

**Files:**
- Create: `src/xana/agent_adapter/channels/feishu_card.py`
- Test: `tests/test_feishu_card.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_feishu_card.py"""
from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft
from xana.agent_adapter.channels.feishu_card import build_card


def _panels(card):
    return [e for e in card["body"]["elements"] if e["tag"] == "collapsible_panel"]


def test_thinking_panel_collapsed_with_icon():
    d = TurnDraft(run_id="r", thinking="step1", content="ans")
    card = build_card(d, finalize=False)
    p = _panels(card)[0]
    assert p["expanded"] is False
    assert p["header"]["title"]["content"] == "💭 思考过程"
    assert p["header"]["icon"]["tag"] == "standard_icon"  # 缺 icon 飞书静默丢面板
    assert p["elements"][0]["content"] == "step1"


def test_tool_panel_header_and_body():
    d = TurnDraft(run_id="r")
    d.tool_blocks.append(ToolBlock(
        call_id="c", name="Bash", args={"command": "ls"},
        running=False, ok=True, result="a.txt", elapsed_ms=7))
    card = build_card(d, finalize=True)
    p = _panels(card)[0]
    assert p["header"]["title"]["content"] == "● Bash(ls) — 7ms"
    body = p["elements"][0]["content"]
    assert '"command": "ls"' in body and "a.txt" in body


def test_answer_footer_buttons_order():
    d = TurnDraft(run_id="r", content="回答",
                  footer_meta=[("model", "m")],
                  buttons=[[{"text": "是", "send": "是"}]])
    card = build_card(d, finalize=True)
    tags = [e["tag"] for e in card["body"]["elements"]]
    assert tags == ["markdown", "hr", "markdown", "column_set"]
    btn = card["body"]["elements"][3]["columns"][0]["elements"][0]
    assert btn["behaviors"][0]["value"] == {"send_text": "是"}


def test_empty_card_placeholder():
    card = build_card(TurnDraft(run_id="r"), finalize=False)
    assert card["body"]["elements"][0] == {"tag": "markdown", "content": " "}
```

- [ ] **Step 2: 跑测试确认失败** → ModuleNotFoundError

- [ ] **Step 3: 实现**:移植 yuxu feishu.py:449-672,差异:工具面板标题用 `blk.header()`(xanac 风格),展开体 = ```json\n{args 美化 json, clip_body}\n``` + 结果(clip_body;运行中显示 `_⏳ 执行中…_`)。结构:

```python
"""src/xana/agent_adapter/channels/feishu_card.py

飞书互动卡片 Schema 2.0 构建。布局(流式与 finalize 结构一致,卡片不跳变):
    [collapsible_panel: 💭 思考过程 — 有 thinking 才有, 始终折叠]
    [collapsible_panel × N: 工具调用 — xanac 风格标题, 始终折叠]
    [markdown: 回答正文]
    [hr + 灰字脚标]
    [column_set × N: 选项按钮]
移植自 yuxu_backup adapters/feishu.py:449-672。collapsible_panel 的
header.icon 必填——缺了飞书静默丢弃整个面板(yuxu 实测注释)。
"""
from __future__ import annotations

import json

from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft, clip_body

_PANEL_HEADER_COMMON = {
    "background_color": "grey-50",
    "vertical_align": "center",
    "padding": "4px 8px 4px 8px",
    "icon": {
        "tag": "standard_icon",
        "token": "down-bold_outlined",
        "color": "grey",
        "size": "16px 16px",
    },
    "icon_position": "follow_text",
    "icon_expanded_angle": -180,
}


def _panel(title: str, body_md: str) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey-50",
        "header": {"title": {"tag": "plain_text", "content": title}, **_PANEL_HEADER_COMMON},
        "elements": [{"tag": "markdown", "content": body_md}],
    }


def _tool_panel(blk: ToolBlock) -> dict:
    parts: list[str] = []
    if blk.args:
        parts.append("```json\n" + clip_body(json.dumps(blk.args, ensure_ascii=False, indent=2)) + "\n```")
    if blk.running:
        parts.append("_⏳ 执行中…_")
    else:
        parts.append(clip_body(blk.result) if blk.result else f"_(no output recorded for `{blk.name}`)_")
    return _panel(blk.header(), "\n".join(parts))


def build_card(draft: TurnDraft, *, finalize: bool) -> dict:
    elements: list[dict] = []
    if draft.thinking:
        elements.append(_panel("💭 思考过程", draft.thinking))
    for blk in draft.tool_blocks:
        elements.append(_tool_panel(blk))
    if draft.content:
        elements.append({"tag": "markdown", "content": draft.content})
    elif not elements:
        elements.append({"tag": "markdown", "content": " "})  # 飞书拒绝空 markdown
    if draft.footer_meta:
        elements.append({"tag": "hr"})
        meta = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{meta}</font>"})
    elements.extend(_buttons_elements(draft.buttons))
    return {"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": elements}}


def _buttons_elements(rows: list[list[dict]]) -> list[dict]:
    # 逐行移植 yuxu _draft_buttons_to_feishu(feishu.py:538-586):
    # 每行一个 column_set,cell 为 weighted column 包一个 button,
    # behaviors=[{"type":"callback","value":{"send_text": send}}]
    out: list[dict] = []
    for row in rows or []:
        cols = []
        for btn in row or []:
            text = str(btn.get("text") or btn.get("send") or "").strip()
            send = str(btn.get("send") or btn.get("text") or "").strip()
            if not text or not send:
                continue
            cols.append({
                "tag": "column", "width": "weighted", "weight": 1,
                "vertical_align": "center",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": text},
                    "type": "default", "size": "medium",
                    "behaviors": [{"type": "callback", "value": {"send_text": send}}],
                }],
            })
        if cols:
            out.append({"tag": "column_set", "horizontal_spacing": "8px", "columns": cols})
    return out
```

(`finalize` 参数当前不影响结构——保留签名以便日后差异化,yuxu 同样如此。)

- [ ] **Step 4: 跑测试** → 4 PASS
- [ ] **Step 5: Commit** `feat(feishu): card builder — collapsible thinking/tool panels, footer, option buttons`

---

### Task 5: FeishuAdapter 状态机(新气泡 + 事件消费)

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu.py`
- Test: `tests/test_feishu_render_turn.py`

先读 feishu.py 现状:`_SessionBuffer`(cumulative_text/last_message_id/coalescer/chat_id)、`render()` 只处理 draft/final/error、`_patch_or_send_now`。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_feishu_render_turn.py

FakeSender 记录 send_card/patch_card 调用。事件用真实 AgentEvent+HarnessEvent
构造(kind/payload/run_id 是关键)。注意 DraftCoalescer 是异步合并——测试里
对每个事件 render 后 `await asyncio.sleep(0)` 不够,直接在断言前调用
adapter 内部 finalize 路径(final 事件本身 await finalize)。
"""
import asyncio

from xana.agent_adapter.channels._bindings import BindingStore
from xana.agent_adapter.channels.feishu import FeishuAdapter, FeishuConfig
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.contracts import HarnessEvent


class FakeSender:
    def __init__(self):
        self.sent = []    # list[(receive_id, card)]
        self.patched = []  # list[(message_id, card)]

    async def send_card(self, *, receive_id, card):
        self.sent.append((receive_id, card))
        return f"mid-{len(self.sent)}"

    async def patch_card(self, *, message_id, card):
        self.patched.append((message_id, card))


def _adapter(tmp_path):
    sender = FakeSender()
    a = FeishuAdapter(
        config=FeishuConfig(app_id="a", app_secret="s", verification_token=""),
        bindings=BindingStore(path=tmp_path / "b.json"),
        api_sender=sender,
    )
    return a, sender


def _ev(kind, session, hkind, payload, run_id="r1", usage=None):
    return AgentEvent(kind=kind, session_id=session, payload=payload,
                      harness_event=HarnessEvent(kind=hkind, session_id=session,
                                                 run_id=run_id, payload=payload,
                                                 usage=usage))


def test_full_turn_renders_one_card_with_blocks(tmp_path):
    async def go():
        a, sender = _adapter(tmp_path)
        await a.bind_session_for_chat("chat1", "s1")
        await a.render(_ev("draft", "s1", "model_delta", {"text": "思", "channel": "thinking"}))
        await a.render(_ev("draft", "s1", "tool_started",
                           {"tool_name": "Bash", "call_id": "c1", "input": {"command": "ls"}}))
        await a.render(_ev("tool", "s1", "tool_finished",
                           {"tool_name": "Bash", "call_id": "c1", "ok": True, "text": "a.txt"}))
        await a.render(_ev("draft", "s1", "model_delta", {"text": "答案"}))
        await a.render(_ev("final", "s1", "final", {"text": "答案\n[选项: 是 | 否]"}))
        card = (sender.patched[-1][1] if sender.patched else sender.sent[-1][1])
        tags = [e["tag"] for e in card["body"]["elements"]]
        assert "collapsible_panel" in tags          # thinking + tool panels
        assert tags.count("collapsible_panel") == 2
        assert any(e.get("content") == "答案" for e in card["body"]["elements"]
                   if e["tag"] == "markdown")        # 按钮行已从正文剥离
        assert tags[-1] == "column_set"              # 按钮在
    asyncio.run(go())


def test_new_run_makes_new_bubble(tmp_path):
    async def go():
        a, sender = _adapter(tmp_path)
        await a.bind_session_for_chat("chat1", "s1")
        await a.render(_ev("draft", "s1", "model_delta", {"text": "x"}, run_id="r1"))
        await a.render(_ev("final", "s1", "final", {"text": "x"}, run_id="r1"))
        await a.render(_ev("draft", "s1", "model_delta", {"text": "y"}, run_id="r2"))
        await a.render(_ev("final", "s1", "final", {"text": "y"}, run_id="r2"))
        assert len(sender.sent) == 2  # 每轮一个新气泡(两次 send_card)
    asyncio.run(go())


def test_final_payload_replaces_content(tmp_path):
    # final 的 payload["text"] 是全量累计文本(harness 语义), content 以它为准
    async def go():
        a, sender = _adapter(tmp_path)
        await a.bind_session_for_chat("chat1", "s1")
        await a.render(_ev("draft", "s1", "model_delta", {"text": "部分"}))
        await a.render(_ev("final", "s1", "final", {"text": "完整答案"}))
        card = (sender.patched or sender.sent)[-1][1]
        md = [e for e in card["body"]["elements"] if e["tag"] == "markdown"]
        assert md[0]["content"] == "完整答案"
    asyncio.run(go())
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现** feishu.py:
  - `_SessionBuffer` 改为持有 `draft: TurnDraft | None`、`message_id: str | None`、`coalescer`、`chat_id`、`tool_started_ts: dict[str, float]`(call_id→monotonic,算 elapsed_ms)。
  - `render(event)` 重写为状态机:
    1. `run_id = event.harness_event.run_id if event.harness_event else None`;若 buffer.draft 为 None 或 `run_id` 非空且 != `buf.draft.run_id` → **新气泡**:`buf.draft = TurnDraft(run_id=run_id or "?")`,`buf.message_id = None`,新 coalescer。
    2. 按 `hk = event.harness_event.kind if event.harness_event else event.kind` 分派:
       - `model_delta`:`payload.get("channel") == "thinking"` → `draft.append_thinking(text)`;否则 `draft.append_content(text)`;然后 `buf.coalescer.flush(send 闭包)`。
       - `tool_started`:`draft.start_tool(call_id=payload["call_id"], name=payload.get("tool_name","?"), args=payload.get("input") or {})`,记 started_ts,flush。
       - `tool_finished`(AgentEvent kind=="tool"):elapsed 从 started_ts 算;`draft.finish_tool(call_id=..., ok=payload.get("ok", True), result=str(payload.get("text","")), elapsed_ms=...)`,flush。
       - `final`:payload text 非空则 `draft.content = text`(全量替换);`draft.content, draft.buttons = extract_buttons(draft.content)`;`draft.footer_meta = build_footer_meta(event.harness_event.usage if event.harness_event else None)`;`await buf.coalescer.finalize(send)`。
       - `error`:`draft.content += f"\n[error] {text}"`;finalize。
       - 其他 harness kind(turn_started/cancelled/paused):忽略(返回 RenderedMessage 即可)。
    3. send 闭包 = `self._patch_or_send_now(buf)`,内部 `build_card(buf.draft, finalize=...)`,message_id None→send_card 否则 patch_card(沿用现有 token/异常处理)。
  - RenderedMessage.text 返回 `draft.content`(保持 gateway 侧约定)。
  - 删除旧的 `cumulative_text`/`_build_footer`/divider 逻辑(被 TurnDraft/feishu_card 取代)。

- [ ] **Step 4: 跑** `python -m pytest tests/test_feishu_render_turn.py tests/ -q -k feishu` → 全 PASS(旧的 feishu 渲染测试若断言旧 markdown 形态,按新卡片结构最小改写,保持断言精神)
- [ ] **Step 5: Commit** `feat(feishu): per-turn card state machine — new bubble per run, thinking/tool panels, buttons`

---

### Task 6: 飞书按钮回流(card.action.trigger)

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu_ws.py`(恢复 card 回调注册)
- Modify: `src/xana/agent_adapter/channels/feishu.py`(处理 card 事件 → 合成 InboundMessage)
- Test: `tests/test_feishu_ws.py`(追加)、`tests/test_feishu_render_turn.py`(追加)

- [ ] **Step 1: 写失败测试**(追加)

```python
# tests/test_feishu_ws.py 追加
def test_card_action_converted_and_bridged():
    """_p2_card_action_to_event_dict(stub) 产出
    {"header":{"event_type":"card.action.trigger"},
     "operator":{"open_id":...}, "action":{"value":{"send_text":...},"tag":...},
     "context":{"open_chat_id":...,"open_message_id":...}};
    FeishuWSClient(on_card_action=cb) 从工作线程桥接调用 cb。
    (参照本文件既有 _on_p2_im_message 桥接测试的写法。)"""
    ...

# tests/test_feishu_render_turn.py 追加
def test_card_action_enqueues_inbound(tmp_path):
    """adapter._on_card_action({...send_text:"是", open_id:"ou_x", open_chat_id:"chat1"})
    → adapter._inbound 队列收到 InboundMessage(text="是", user_id="ou_x",
    session_id 来自 bindings.allocate("chat1"))。未配对用户同样走 pairing 拦截
    (复用 _on_event 的 pairing 分支——直接断言:pairing 拒绝时不入队)。"""
    ...
```

(实现者:`...` 体按两文件中既有测试的构造方式补全,docstring 是约束。)

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**
  - feishu_ws.py:照 yuxu feishu_ws.py:145-147 + 192-296 恢复 `on_card_action: Optional[EventCallback]` 构造参数、`register_p2_card_action_trigger`(仅当回调非 None)、`_on_p2_card_action`(返回 `P2CardActionTriggerResponse({})`,lazy import,yuxu 原样)、`_p2_card_action_to_event_dict`(yuxu :257-296 原样)。
  - feishu.py:新增 `async _on_card_action(self, event: dict)`:取 `action.value.send_text`/`operator.open_id`/`context.open_chat_id`;空 send_text 直接返回;**复用 pairing 检查**(把 `_on_event` 里 pairing 分支抽成 `_check_pairing(user_id, chat_id, text) -> bool` 供两处用);通过后 `bindings.allocate(chat_id)` → `InboundMessage(text=send_text, user_id=open_id, metadata={"chat_id": chat_id, "source": "card_action"})` 入 `_inbound` 队列。
  - feishu.py `start()` WS 模式:`FeishuWSClient(..., on_im_message=self._on_event, on_card_action=self._on_card_action)`。

- [ ] **Step 4: 跑** `python -m pytest tests/test_feishu_ws.py tests/test_feishu_render_turn.py -v` → 全 PASS
- [ ] **Step 5: Commit** `feat(feishu): card button clicks re-enter gateway as user messages (WS card.action.trigger)`

---

### Task 7: Telegram HTML 渲染器 + 状态机 + finalize 拆分 + callback

**Files:**
- Create: `src/xana/agent_adapter/channels/telegram_html.py`
- Modify: `src/xana/agent_adapter/channels/telegram.py`
- Test: `tests/test_telegram_html.py`、`tests/test_telegram_render_turn.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_telegram_html.py"""
from xana.agent_adapter.channels.telegram_html import (
    clip_html, md_to_html, render_streaming, render_thinking, render_tool_block,
)
from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft


def test_md_to_html_escapes_and_converts():
    out = md_to_html("**粗** `code` <tag>")
    assert "<b>粗</b>" in out and "<code>code</code>" in out and "&lt;tag&gt;" in out


def test_render_thinking_expandable():
    html = render_thinking("步骤")
    assert html.startswith("💭 <b>Thinking</b>")
    assert "<blockquote expandable>" in html and "步骤" in html


def test_render_tool_block_running_then_done():
    blk = ToolBlock(call_id="c", name="Bash", args={"command": "ls"})
    running = render_tool_block(blk)
    assert "⏳ 执行中" in running and "<blockquote expandable>" in running
    blk.running, blk.result, blk.elapsed_ms = False, "a.txt", 7
    done = render_tool_block(blk)
    assert "7ms" in done and "a.txt" in done


def test_render_streaming_thinking_above_content():
    d = TurnDraft(run_id="r", thinking="t", content="c")
    html = render_streaming(d)
    assert html.index("t") < html.index(">c") or html.index("Thinking") < html.index("c")


def test_clip_html_under_4096():
    assert len(clip_html("x" * 9000)) <= 4096
```

```python
"""tests/test_telegram_render_turn.py

FakeApiClient 记录 send_message/edit_message_text(带 reply_markup 透传——
注意:现有 TelegramApiClient 协议的 send_message/edit_message_text 没有
reply_markup 参数,本任务给协议类与 HttpxTelegramClient 加上
`reply_markup: dict | None = None`(telegram.py 的协议类不是锁定文件)。)

场景断言(事件构造同 tests/test_feishu_render_turn.py 的 _ev helper):
1. test_streaming_single_message_grows: 两个 model_delta + finalize(无 thinking,
   无工具) → 第一次 send_message,后续 edit 同一 message_id;final 后总 send 数 == 1。
2. test_tool_block_is_sealed_message: tool_started → 新 send(含 ⏳ 占位);
   tool_finished(同 call_id)→ 对该占位消息 edit(含结果),不再新 send。
3. test_finalize_split_with_thinking: thinking 增量 + 正文 + final →
   流式消息被 edit 成 thinking HTML(blockquote expandable),answer 是新 send,
   且 answer 的 reply_markup 含按钮([选项: 是 | 否] 提取)。
4. test_callback_query_enqueues_inbound: 构造 telegram update
   {"callback_query": {"id":"q","from":{"id":1},"data":"btn:0:0",
    "message":{"chat":{"id":99}}}} → _dispatch_update 后 _inbound 收到
   text=="是"(按钮文本从 adapter 的 per-chat 按钮缓存取);
   并调用了 answerCallbackQuery(FakeApiClient 记录)。
5. test_new_run_new_bubble: 两轮 final(不同 run_id)→ 两次独立 send。
"""
...
```

(实现者:测试体按 telegram.py 既有测试 + feishu 对应测试样式补全;5 个场景 docstring 为约束。)

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

`telegram_html.py`(移植 yuxu telegram.py:599-660 思路,简化):

```python
"""src/xana/agent_adapter/channels/telegram_html.py

Telegram HTML parse-mode 渲染。thinking/工具块用 <blockquote expandable>
(展开一 tap,默认折叠——与飞书 collapsible_panel 同 UX)。
单消息 4096 字符上限,clip_html 预留标签余量截到 3900。
"""
from __future__ import annotations

import html as _html
import json
import re

from xana.agent_adapter.channels._draft import ToolBlock, TurnDraft, clip_body

_TG_LIMIT = 3900  # 4096 减 HTML 标签余量


def md_to_html(text: str) -> str:
    """极简 markdown→Telegram HTML: 先转义,再 **bold** / `code` / ```pre```。"""
    s = _html.escape(text or "")
    s = re.sub(r"```(?:\w*\n)?(.*?)```", r"<pre>\1</pre>", s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    return s


def clip_html(s: str, limit: int = _TG_LIMIT) -> str:
    return s if len(s) <= limit else s[:limit] + "…"


def render_thinking(thinking: str) -> str:
    return ("💭 <b>Thinking</b>\n"
            f"<blockquote expandable>{md_to_html(clip_body(thinking))}</blockquote>")


def render_tool_block(blk: ToolBlock) -> str:
    head = _html.escape(blk.header())
    body_parts: list[str] = []
    if blk.args:
        body_parts.append("<pre>" + _html.escape(
            clip_body(json.dumps(blk.args, ensure_ascii=False, indent=2))) + "</pre>")
    if blk.running:
        body_parts.append("⏳ 执行中…")
    elif blk.result:
        body_parts.append(md_to_html(clip_body(blk.result)))
    return clip_html(f"<b>{head}</b>\n<blockquote expandable>" + "\n".join(body_parts) + "</blockquote>")


def render_streaming(draft: TurnDraft) -> str:
    """流式中的单消息: [thinking 折叠块] + 正文。工具块不在此(独立 sealed 消息)。"""
    parts: list[str] = []
    if draft.thinking:
        parts.append(render_thinking(draft.thinking))
    if draft.content:
        parts.append(md_to_html(draft.content))
    return clip_html("\n".join(parts) or "…")


def render_answer(draft: TurnDraft) -> str:
    parts: list[str] = []
    if draft.content:
        parts.append(md_to_html(draft.content))
    if draft.footer_meta:
        meta = " | ".join(f"{k}: {v}" for k, v in draft.footer_meta)
        parts.append("――――――――――――\n<i>" + _html.escape(meta) + "</i>")
    return clip_html("\n".join(parts).strip() or " ")


def buttons_to_reply_markup(rows: list[list[dict]]) -> dict | None:
    """[[{text,send}],...] → InlineKeyboardMarkup。callback_data 用 'btn:{r}:{c}'
    索引(64 字节限制,真实文本由 adapter 缓存按 chat 维护)。"""
    keyboard = [[{"text": b["text"][:64], "callback_data": f"btn:{r}:{c}"}
                 for c, b in enumerate(row)] for r, row in enumerate(rows or []) if row]
    return {"inline_keyboard": keyboard} if keyboard else None
```

`telegram.py` 改造:
  - 协议类 `TelegramApiClient.send_message/edit_message_text` 增加 `reply_markup: dict | None = None` 形参;`HttpxTelegramClient` 实现透传到 body;新增协议方法 `answer_callback_query(callback_query_id: str) -> None`(Httpx 实现 POST `/answerCallbackQuery`;失败 log.debug 吞掉)。
  - `_SessionBuffer` → 持有 `draft: TurnDraft | None`、`message_id: int | None`(流式消息)、`coalescer`、`chat_id`、`tool_msg_ids: dict[str, int]`(call_id→sealed 消息 id)、`tool_started_ts: dict[str, float]`、`last_buttons: list[list[dict]]`(callback 文本反查)。
  - `render()` 状态机同 Task 5 框架(run_id 换轮重置;model_delta thinking/content;final 全量替换 + extract_buttons + build_footer_meta + finalize),平台差异:
    - 流式 flush 闭包:`render_streaming(draft)` → 首次 send / 之后 edit(parse_mode="HTML")。
    - `tool_started`:不进流式消息;立即(不走 coalescer)`send_message(render_tool_block(blk))`,message_id 记入 `tool_msg_ids[call_id]`。
    - `tool_finished`:更新块后,`edit_message_text(tool_msg_ids[call_id], render_tool_block(blk))`;没有占位则 send。
    - `final` 拆分(yuxu telegram.py:339-395 语义):有 thinking → 先 `await coalescer.finalize(空 send)` 停掉尾随,流式消息 edit 成 `render_thinking(...)`,再 send `render_answer(...)`(reply_markup=按钮);无 thinking → 流式消息 edit 成 answer(带按钮),没有流式消息则 send;answer 为空且有流式消息 → edit 成 " " 清殻。按钮存 `buf.last_buttons`。
    - `_handle_callback_query`(`_dispatch_update` 里 `update.get("callback_query")` 分支,放在 message 分支之前):data 形如 `btn:{r}:{c}` → 从该 chat 对应 buffer 的 `last_buttons[r][c]["send"]` 取文本;走与普通消息一致的 pairing/allowed 检查后合成 InboundMessage 入队;最后 `answer_callback_query(id)`。取不到文本(轮换后)→ 仅 answer,不入队。
  - 删除旧 cumulative_text/_build_footer/divider 逻辑。

- [ ] **Step 4: 跑** `python -m pytest tests/test_telegram_html.py tests/test_telegram_render_turn.py tests/ -q -k telegram` → 全 PASS(旧 telegram 渲染测试按新结构最小改写)
- [ ] **Step 5: Commit** `feat(telegram): HTML turn renderer — sealed tool messages, finalize split, inline buttons`

---

### Task 8: Phase 1 收尾回归 + 人工冒烟点

- [ ] **Step 1:** `python -m pytest -q` → 除已知失败全过
- [ ] **Step 2:** `git log --name-only -10` 确认未触碰锁定文件(`config/contracts.py` 此阶段不应出现)
- [ ] **Step 3:** 留给人工:`~/.xana/bin/xana sync`(自动重启 serve)后,飞书/tg 各发一条带工具调用的消息,验证:新气泡、thinking 折叠、工具折叠(标题同 xanac)、`[选项: ...]` 成按钮、点按钮回流、脚标含 `context: x/967k`。
- [ ] **Step 4:** 若 Step 1-2 产生零散修改,commit `chore: phase 1 regression fixes`

---

## Spec 覆盖表(Phase 1)

| 需求 | 任务 |
|---|---|
| 每轮新气泡(不复用 message_id) | 5(feishu)、7(tg) |
| thinking 默认折叠块 | 3(channel)、4/5(feishu)、7(tg) |
| 工具折叠块,标题同 xanac,内容全量 args+结果 | 1、2、4、5、7 |
| 回答不折叠 | 4、5、7 |
| 模型选项渲染为按钮 + 点击回流 | 2、5、6、7 |
| 脚标恢复 + token/上下文预算(967k 占位) | 2、5、7 |
