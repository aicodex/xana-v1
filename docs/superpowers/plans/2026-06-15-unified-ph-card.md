# Unified PH Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同 PR 落地统一 PH 卡片模板(feishu + telegram 同步),删旧 `build_fg_streaming_card` / `build_bg_placeholder_card` / 老 compact 卡 builder。新模板覆盖 chat / compact / detach / menu / `/attach` 所有场景。

**Architecture:** 平台无关的 `UnifiedCardState` 数据模型 + 状态机(事件 → state patch)+ 两个平台特定 renderer(feishu schema / TG HTML)。每张卡 = 1 PH(或菜单卡);只 edit message_id,不 recreate。

**Tech Stack:** Python 3.12 / dataclasses / asyncio / 现有 feishu(lark)+ telegram(bot API)。

**上游 spec:** `docs/superpowers/specs/2026-06-15-unified-ph-card-design.md`。**冲突以 spec 为准。**

**前置:** P5 已上线(commit `1c40d20` 及之前);`xana sync` 已 sync 过新代码。

---

## File Structure

**新增:**
- `src/xana/agent_adapter/channels/unified_card.py` — `UnifiedCardState` + 子 section dataclasses + state machine 入口
- `src/xana/agent_adapter/channels/unified_card_feishu.py` — feishu 卡 schema renderer
- `src/xana/agent_adapter/channels/unified_card_telegram.py` — telegram HTML renderer

**修改:**
- `src/xana/agent_adapter/contracts.py` — 扩 ProgressEvent kind 枚举(加 `compact_started` / `menu_response`)
- `src/xana/agent_adapter/channels/feishu.py` — 用 `unified_card_feishu.render(state)` 替换 `_patch_fg_*` / `_complete_bg_card` 等;删旧 build_* 引用
- `src/xana/agent_adapter/channels/telegram.py` — 同上
- `src/xana/agent_adapter/commands.py` — `_cmd_compact` / `_cmd_help` / `_cmd_attach` 用新事件 kind
- `src/xana/agent_adapter/gateway.py` — `detach_foreground` 触发 `run_detached` 事件(已存在,检查 payload)

**删除(同 PR):**
- `src/xana/agent_adapter/channels/feishu_card.py` 里:`build_fg_streaming_card` / `build_bg_placeholder_card` / 所有 `_panel` 之外的 builder
- `src/xana/agent_adapter/channels/telegram_html.py` 同等价路径
- 老测试:`tests/test_feishu_render_turn.py` 中针对老 builder 的、`tests/test_feishu_dual_card.py` 中针对 bg_placeholder 的等

**新测试:**
- `tests/test_unified_card_state_machine.py`
- `tests/test_unified_card_compact_section.py`
- `tests/test_unified_card_answer_section.py`
- `tests/test_unified_card_tools_block.py`
- `tests/test_unified_card_footer.py`
- `tests/test_unified_card_buttons.py`
- `tests/test_unified_card_feishu_render.py`
- `tests/test_unified_card_telegram_render.py`
- `tests/test_unified_card_detach_transition.py`
- `tests/test_unified_card_ph_replaced.py`
- `tests/test_unified_card_menu_section.py`

---

## Task 0 — 接口改动审批门

**File approvals required(实施 agent 先与用户确认才开 Task 1):**

1. `src/xana/agent_adapter/contracts.py` — 给 `ProgressEvent`(若已有 kind 字段)或单独枚举加新 kind:
   - `compact_started`(compact run 启动)
   - `menu_response`(纯命令响应,无 PH 关联)
   - `attach_options`(`/attach` 命令的底部按钮 payload)

2. `src/xana/agent_adapter/interfaces.py` — `Adapter` ABC 加可选方法 `render_card_state(state: UnifiedCardState) -> RenderedMessage`(默认 fallback 到旧 `render(event)` 路径,保证非 feishu/telegram adapter 不破)?
   **或者**:不动 ABC,只在 feishu/telegram 实现里多走一条路径。**优选后者**(零接口改动)。

3. 用户确认上述两点后 → Task 1 开始。

- [ ] **Step 0.1: 列改动给用户审批,等明确"OK 开 Task 1"**

---

## Task 1 — `UnifiedCardState` 数据模型

**Files:**
- Create: `src/xana/agent_adapter/channels/unified_card.py`
- Test: `tests/test_unified_card_state_machine.py`

### Step 1.1 — 写最小数据模型测试

```python
# tests/test_unified_card_state_machine.py
"""UnifiedCardState 数据模型:section 可选,默认空状态可构造。"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, FooterText,
)


def test_empty_card_state_constructible():
    state = UnifiedCardState(card_id="c1")
    assert state.card_id == "c1"
    assert state.message_id is None
    assert state.compact is None
    assert state.answer is None
    assert state.menu is None
    assert state.top_buttons == ()
    assert state.bottom_buttons == ()
    assert state.detached is False
    assert state.terminal is False


def test_compact_section_defaults():
    sec = CompactSection()
    assert sec.state == "input"          # input / streaming / completed / cancelled
    assert sec.progress_cells == 0        # 0..10
    assert sec.compacted_n == 0
    assert sec.summary_text == ""


def test_answer_section_defaults():
    sec = AnswerSection()
    assert sec.state == "streaming"      # streaming / detached / terminal / cancelled
    assert sec.thinking_text == ""
    assert sec.tools == ()
    assert sec.tools_pending == 0
    assert sec.answer_text == ""


def test_footer_renders_fg_running():
    f = FooterText(model="claude-opus-4-7", ctx_used=12000, ctx_max=200000,
                   elapsed_s=65, state="fg_running")
    assert "claude-opus-4-7" in f.render()
    assert "上下文 12000/200000" in f.render()
    assert "1m 5s" in f.render()
```

- [ ] **Step 1.1: 写测试**

- [ ] **Step 1.2: 跑确认失败**

Run: `pytest tests/test_unified_card_state_machine.py -v`
Expected: FAIL — 模块不存在。

- [ ] **Step 1.3: 写 `unified_card.py` 数据模型**

```python
# src/xana/agent_adapter/channels/unified_card.py
"""统一 PH 卡片模型(spec: 2026-06-15-unified-ph-card-design.md)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CompactState = Literal["input", "streaming", "completed", "cancelled"]
AnswerState = Literal["streaming", "detached", "terminal", "cancelled"]
FooterState = Literal["fg_running", "bg_frozen", "terminal"]


@dataclass
class ToolBlock:
    name: str
    header: str                  # 例 "Read /foo/bar.py"
    body_md: str
    ok: bool = True


@dataclass
class CompactSection:
    state: CompactState = "input"
    progress_cells: int = 0       # 0..10
    compacted_n: int = 0
    summary_text: str = ""


@dataclass
class AnswerSection:
    state: AnswerState = "streaming"
    thinking_text: str = ""
    tools: tuple[ToolBlock, ...] = ()
    tools_pending: int = 0        # 待加载工具数 (累积 - 已显示)
    answer_text: str = ""


@dataclass
class MenuSection:
    body_md: str = ""             # /help 等纯命令响应内容


@dataclass
class ButtonRow:
    buttons: tuple["Button", ...]


@dataclass
class Button:
    text: str
    send: str | None = None       # 点击发送的命令


def _format_elapsed(secs: int) -> str:
    if secs < 0:
        secs = 0
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    parts = []
    if h > 0:
        parts.append(f"{h}h")
        parts.append(f"{m}m")
        parts.append(f"{s}s")
    elif m > 0:
        parts.append(f"{m}m")
        parts.append(f"{s}s")
    else:
        parts.append(f"{s}s")
    return " ".join(parts)


@dataclass
class FooterText:
    model: str = ""
    ctx_used: int = 0
    ctx_max: int = 0
    elapsed_s: int = 0
    state: FooterState = "fg_running"

    def render(self) -> str:
        t = _format_elapsed(self.elapsed_s)
        if self.state == "bg_frozen":
            return f"{self.model} · 🟡 后台运行中 · 用时 {t}"
        return f"{self.model} · 上下文 {self.ctx_used}/{self.ctx_max} · 用时 {t}"


@dataclass
class UnifiedCardState:
    card_id: str
    message_id: str | None = None
    ph_run_id: str | None = None
    top_buttons: tuple[ButtonRow, ...] = ()
    compact: CompactSection | None = None
    answer: AnswerSection | None = None
    menu: MenuSection | None = None
    bottom_buttons: tuple[ButtonRow, ...] = ()
    footer: FooterText = field(default_factory=FooterText)
    detached: bool = False
    terminal: bool = False
```

- [ ] **Step 1.4: 跑测试通过**

Run: `pytest tests/test_unified_card_state_machine.py -v`
Expected: 4 PASS。

- [ ] **Step 1.5: Commit**

```bash
git add src/xana/agent_adapter/channels/unified_card.py
git commit -m "feat(card): UnifiedCardState data model (unified PH card)"
cd tests && git add test_unified_card_state_machine.py && \
  git commit -m "test(card): UnifiedCardState data model basics" && cd ..
```

---

## Task 2 — Footer formatter + 时间格式

**Files:**
- Test: `tests/test_unified_card_footer.py`
- 实现已在 Task 1 `_format_elapsed` 完成。

### Step 2.1 — 写时间格式 + 三态测试

```python
# tests/test_unified_card_footer.py
"""脚标三态 + 时间格式 1h 2m 1s / 5s / 1m 5s。"""
import pytest
from xana.agent_adapter.channels.unified_card import FooterText, _format_elapsed


@pytest.mark.parametrize("secs,want", [
    (0, "0s"), (5, "5s"), (65, "1m 5s"),
    (3661, "1h 1m 1s"), (7200, "2h 0m 0s"),
    (-3, "0s"),  # 负数兜底
])
def test_format_elapsed(secs, want):
    assert _format_elapsed(secs) == want


def test_footer_fg_running():
    f = FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5, state="fg_running")
    assert f.render() == "m · 上下文 10/100 · 用时 5s"


def test_footer_bg_frozen_hides_ctx():
    f = FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5, state="bg_frozen")
    rendered = f.render()
    assert "🟡 后台运行中" in rendered
    assert "10/100" not in rendered


def test_footer_terminal_same_as_fg():
    f1 = FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5, state="fg_running")
    f2 = FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5, state="terminal")
    assert f1.render() == f2.render()
```

- [ ] **Step 2.1: 写测试 + 跑确认通过**

Run: `pytest tests/test_unified_card_footer.py -v`
Expected: 9 PASS(6 parametrize + 3 case)。

- [ ] **Step 2.2: Commit**

```bash
cd tests && git add test_unified_card_footer.py && \
  git commit -m "test(card): footer 3-state + elapsed format" && cd ..
```

---

## Task 3 — Compact section 进度条状态机

**Files:**
- Modify: `src/xana/agent_adapter/channels/unified_card.py`(加 progress 推进函数)
- Test: `tests/test_unified_card_compact_section.py`

### Step 3.1 — 写测试

```python
# tests/test_unified_card_compact_section.py
"""compact 段进度条状态机:
- input 阶段按时间 0→7;首 delta 跳 7
- streaming 阶段按 SUMMARY_SECTIONS(7 个)推进 7→10(每段 0.43,floor)
- completed:10 格
- cancelled:进度隐藏(progress_cells = 0 占位)
"""
import pytest
from xana.agent_adapter.channels.unified_card import (
    CompactSection, advance_compact_input, advance_compact_streaming,
    finalize_compact, cancel_compact,
)


def test_input_starts_at_0_cells():
    sec = CompactSection()
    assert sec.state == "input"
    assert sec.progress_cells == 0


def test_input_time_advance_caps_at_7():
    sec = CompactSection()
    # 假设 elapsed=20s → 7 格;>=20 不超 7
    advance_compact_input(sec, elapsed_s=10.0)
    assert sec.progress_cells == 3       # floor(10/20*7) = 3
    advance_compact_input(sec, elapsed_s=25.0)
    assert sec.progress_cells == 7


def test_first_delta_jumps_to_7():
    sec = CompactSection()
    advance_compact_streaming(sec, sections_done=0)
    assert sec.state == "streaming"
    assert sec.progress_cells == 7


def test_streaming_sections_done_advance():
    sec = CompactSection(state="streaming", progress_cells=7)
    advance_compact_streaming(sec, sections_done=3)
    # 7 + floor(3/7 * 3) = 7 + 1 = 8
    assert sec.progress_cells == 8
    advance_compact_streaming(sec, sections_done=7)
    assert sec.progress_cells == 10


def test_finalize_sets_completed():
    sec = CompactSection(state="streaming", progress_cells=8)
    finalize_compact(sec, compacted_n=42, summary_text="...")
    assert sec.state == "completed"
    assert sec.progress_cells == 10
    assert sec.compacted_n == 42


def test_cancel_resets_progress():
    sec = CompactSection(state="streaming", progress_cells=5)
    cancel_compact(sec)
    assert sec.state == "cancelled"
```

- [ ] **Step 3.1: 写测试 + 跑确认失败**

Run: `pytest tests/test_unified_card_compact_section.py -v`
Expected: FAIL — 函数不存在。

- [ ] **Step 3.2: 实现状态机函数**

在 `unified_card.py` 末尾加:

```python
def advance_compact_input(sec: CompactSection, elapsed_s: float) -> None:
    """input 阶段按时间推进 0→7。20s 估算总长。"""
    if sec.state != "input":
        return
    sec.progress_cells = min(7, int(elapsed_s / 20.0 * 7))


def advance_compact_streaming(sec: CompactSection, sections_done: int) -> None:
    """streaming 阶段从 7 起,SUMMARY_SECTIONS=7,每段 0.43 → floor(N/7*3)。"""
    sec.state = "streaming"
    sec.progress_cells = min(10, 7 + int(sections_done / 7 * 3))


def finalize_compact(sec: CompactSection, *, compacted_n: int, summary_text: str) -> None:
    sec.state = "completed"
    sec.progress_cells = 10
    sec.compacted_n = compacted_n
    sec.summary_text = summary_text


def cancel_compact(sec: CompactSection) -> None:
    sec.state = "cancelled"
```

- [ ] **Step 3.3: 测试通过 + commit**

Run: `pytest tests/test_unified_card_compact_section.py -v`
Expected: 6 PASS。

```bash
git add src/xana/agent_adapter/channels/unified_card.py
git commit -m "feat(card): compact section progress state machine"
cd tests && git add test_unified_card_compact_section.py && \
  git commit -m "test(card): compact progress 0->7->10 advancement" && cd ..
```

---

## Task 4 — Answer section + tools 块溢出逻辑

**Files:**
- Modify: `src/xana/agent_adapter/channels/unified_card.py`
- Test: `tests/test_unified_card_answer_section.py`, `tests/test_unified_card_tools_block.py`

### Step 4.1 — 写 answer 测试

```python
# tests/test_unified_card_answer_section.py
"""answer 段状态机:streaming → detached → terminal/cancelled。"""
from xana.agent_adapter.channels.unified_card import (
    AnswerSection, ToolBlock, append_tool, append_thinking, append_answer,
    detach_answer, finalize_answer, cancel_answer,
)


def test_streaming_starts_empty():
    sec = AnswerSection()
    assert sec.state == "streaming"
    assert sec.thinking_text == ""
    assert sec.tools == ()
    assert sec.answer_text == ""


def test_append_thinking_accumulates():
    sec = AnswerSection()
    append_thinking(sec, "想 ")
    append_thinking(sec, "啊")
    assert sec.thinking_text == "想 啊"


def test_append_tool_appends():
    sec = AnswerSection()
    append_tool(sec, ToolBlock(name="Read", header="Read /a", body_md="...", ok=True))
    assert len(sec.tools) == 1


def test_append_answer_accumulates():
    sec = AnswerSection()
    append_answer(sec, "hello ")
    append_answer(sec, "world")
    assert sec.answer_text == "hello world"


def test_detach_flips_state():
    sec = AnswerSection()
    append_answer(sec, "partial")
    detach_answer(sec)
    assert sec.state == "detached"
    # 内容保留(detach 不丢)
    assert sec.answer_text == "partial"


def test_finalize_terminal():
    sec = AnswerSection(state="streaming")
    finalize_answer(sec, final_text="done")
    assert sec.state == "terminal"
    assert sec.answer_text == "done"


def test_cancel_state():
    sec = AnswerSection()
    cancel_answer(sec)
    assert sec.state == "cancelled"
```

- [ ] **Step 4.1: 写 answer 测试**

### Step 4.2 — 写 tools 块溢出测试

```python
# tests/test_unified_card_tools_block.py
"""tools 块默认显前 3,超出累积到 tools_pending。"""
from xana.agent_adapter.channels.unified_card import (
    AnswerSection, ToolBlock, append_tool, load_pending_tools, TOOLS_VISIBLE_LIMIT,
)


def test_tools_visible_limit_is_3():
    assert TOOLS_VISIBLE_LIMIT == 3


def test_tools_within_limit_no_pending():
    sec = AnswerSection()
    for i in range(3):
        append_tool(sec, ToolBlock(name=f"t{i}", header="h", body_md="b", ok=True))
    assert len(sec.tools) == 3
    assert sec.tools_pending == 0


def test_tools_overflow_accumulates_pending():
    sec = AnswerSection()
    for i in range(5):
        append_tool(sec, ToolBlock(name=f"t{i}", header="h", body_md="b", ok=True))
    assert len(sec.tools) == 3       # 仍显示前 3
    assert sec.tools_pending == 2     # 待加载 2


def test_load_pending_reveals_all():
    sec = AnswerSection()
    for i in range(5):
        append_tool(sec, ToolBlock(name=f"t{i}", header="h", body_md="b", ok=True))
    load_pending_tools(sec)
    assert len(sec.tools) == 5
    assert sec.tools_pending == 0
```

- [ ] **Step 4.2: 写 tools 测试 + 跑确认失败**

Run: `pytest tests/test_unified_card_answer_section.py tests/test_unified_card_tools_block.py -v`
Expected: FAIL — 函数不存在。

- [ ] **Step 4.3: 在 unified_card.py 加实现**

```python
TOOLS_VISIBLE_LIMIT = 3


def append_thinking(sec: AnswerSection, delta: str) -> None:
    sec.thinking_text += delta


def append_tool(sec: AnswerSection, tool: ToolBlock) -> None:
    total = len(sec.tools) + sec.tools_pending
    if total < TOOLS_VISIBLE_LIMIT:
        sec.tools = sec.tools + (tool,)
    else:
        # 已显示 ≥ 3,新工具入 pending
        sec.tools_pending += 1
        # 但内部仍要保留以便 load_pending 时呈现 — 把 tool 暂存
        # 用一个内部缓冲 list,与 sec.tools 区分
        if not hasattr(sec, "_pending_tools_buf"):
            sec._pending_tools_buf = []  # type: ignore[attr-defined]
        sec._pending_tools_buf.append(tool)  # type: ignore[attr-defined]


def load_pending_tools(sec: AnswerSection) -> None:
    buf = getattr(sec, "_pending_tools_buf", [])
    if buf:
        sec.tools = sec.tools + tuple(buf)
    sec.tools_pending = 0
    if hasattr(sec, "_pending_tools_buf"):
        del sec._pending_tools_buf  # type: ignore[attr-defined]


def append_answer(sec: AnswerSection, delta: str) -> None:
    sec.answer_text += delta


def detach_answer(sec: AnswerSection) -> None:
    sec.state = "detached"


def finalize_answer(sec: AnswerSection, *, final_text: str) -> None:
    sec.state = "terminal"
    sec.answer_text = final_text


def cancel_answer(sec: AnswerSection) -> None:
    sec.state = "cancelled"
```

- [ ] **Step 4.4: 测试通过 + commit**

Run: `pytest tests/test_unified_card_answer_section.py tests/test_unified_card_tools_block.py -v`
Expected: 11 PASS。

```bash
git add src/xana/agent_adapter/channels/unified_card.py
git commit -m "feat(card): answer section + tools overflow buffering"
cd tests && git add test_unified_card_answer_section.py test_unified_card_tools_block.py && \
  git commit -m "test(card): answer/tools state transitions + overflow" && cd ..
```

---

## Task 5 — 按钮规则 + detach 瞬间 patch + ph_replaced 终态

**Files:**
- Modify: `src/xana/agent_adapter/channels/unified_card.py`
- Test: `tests/test_unified_card_buttons.py`, `tests/test_unified_card_detach_transition.py`, `tests/test_unified_card_ph_replaced.py`

### Step 5.1 — 写按钮规则测试

```python
# tests/test_unified_card_buttons.py
"""顶部按钮:fg+answer-running → 停止/后台;compact 同跑不可取消;后台无按钮。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, compute_top_buttons,
)


def test_fg_answer_running_shows_stop_and_bg():
    state = UnifiedCardState(card_id="c1", answer=AnswerSection(state="streaming"))
    btns = compute_top_buttons(state)
    labels = [b.text for row in btns for b in row.buttons]
    assert "停止" in labels[0]
    assert "后台" in labels[1]


def test_fg_compact_only_shows_cancel():
    state = UnifiedCardState(card_id="c1", compact=CompactSection(state="streaming"))
    btns = compute_top_buttons(state)
    labels = [b.text for row in btns for b in row.buttons]
    assert any("取消" in l for l in labels)


def test_fg_compact_plus_answer_running_no_compact_cancel():
    state = UnifiedCardState(card_id="c1",
                              compact=CompactSection(state="streaming"),
                              answer=AnswerSection(state="streaming"))
    btns = compute_top_buttons(state)
    labels = [b.text for row in btns for b in row.buttons]
    # 顶部只 显示 停止/后台,无 compact 取消(取消了立马被唤醒)
    assert "停止" in "".join(labels)
    assert "后台" in "".join(labels)
    assert "取消" not in "".join(labels)


def test_detached_state_no_top_buttons():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="streaming"),
                              detached=True)
    btns = compute_top_buttons(state)
    assert btns == ()


def test_terminal_state_no_top_buttons():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="terminal"),
                              terminal=True)
    btns = compute_top_buttons(state)
    assert btns == ()
```

- [ ] **Step 5.1: 写按钮测试**

### Step 5.2 — 写 detach transition 测试

```python
# tests/test_unified_card_detach_transition.py
"""detach 瞬间:answer 段折叠 + 占位'回答中…';tools 折叠;顶部按钮清空;脚标→bg_frozen。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, AnswerSection, ToolBlock, FooterText,
    apply_detach,
)


def test_apply_detach_sets_flags_and_footer():
    state = UnifiedCardState(
        card_id="c1",
        answer=AnswerSection(state="streaming", answer_text="partial...",
                              tools=(ToolBlock(name="Read", header="h", body_md="b"),)),
        footer=FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5,
                          state="fg_running"),
    )
    apply_detach(state)
    assert state.detached is True
    assert state.answer.state == "detached"
    assert state.footer.state == "bg_frozen"
    # 内容保留(只 state 变,渲染时按 state 折叠)
    assert state.answer.answer_text == "partial..."
    assert len(state.answer.tools) == 1
```

- [ ] **Step 5.2: 写 detach 测试**

### Step 5.3 — 写 ph_replaced 终态测试

```python
# tests/test_unified_card_ph_replaced.py
"""ph_replaced 终态:answer 段 → terminal,内容替换为最终文本;脚标 → terminal(同 fg_running 格式)。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, AnswerSection, FooterText, apply_ph_replaced,
)


def test_ph_replaced_terminalizes():
    state = UnifiedCardState(
        card_id="c1",
        answer=AnswerSection(state="detached", answer_text="incomplete..."),
        footer=FooterText(model="m", ctx_used=10, ctx_max=100, elapsed_s=5,
                          state="bg_frozen"),
        detached=True,
    )
    apply_ph_replaced(state, final_answer="完整回答", ctx_used=12000, elapsed_s=72)
    assert state.terminal is True
    assert state.answer.state == "terminal"
    assert state.answer.answer_text == "完整回答"
    assert state.footer.state == "terminal"
    assert state.footer.ctx_used == 12000
    assert state.footer.elapsed_s == 72
```

- [ ] **Step 5.3: 写 ph_replaced 测试 + 跑确认 3 个测试都失败**

Run: `pytest tests/test_unified_card_buttons.py tests/test_unified_card_detach_transition.py tests/test_unified_card_ph_replaced.py -v`
Expected: FAIL — 函数不存在。

- [ ] **Step 5.4: 在 unified_card.py 加实现**

```python
def compute_top_buttons(state: UnifiedCardState) -> tuple[ButtonRow, ...]:
    if state.detached or state.terminal:
        return ()
    a_running = state.answer is not None and state.answer.state == "streaming"
    c_running = state.compact is not None and state.compact.state in ("input", "streaming")
    if a_running:
        # answer 在跑(无论 compact 是否在跑):显示 停止/后台。compact 取消屏蔽。
        return (ButtonRow(buttons=(
            Button(text="⏹ 停止", send=f"/cancel {state.ph_run_id or ''}".strip()),
            Button(text="➡ 后台", send=None),  # send=None 表示 adapter 本地处理 detach
        )),)
    if c_running:
        # 仅 compact 在跑(无 answer):可取消
        return (ButtonRow(buttons=(
            Button(text="取消", send=f"/compact_cancel {state.ph_run_id or ''}".strip()),
        )),)
    return ()


def apply_detach(state: UnifiedCardState) -> None:
    state.detached = True
    if state.answer is not None:
        state.answer.state = "detached"
    state.footer.state = "bg_frozen"
    state.top_buttons = ()


def apply_ph_replaced(state: UnifiedCardState, *, final_answer: str,
                      ctx_used: int, elapsed_s: int) -> None:
    state.terminal = True
    state.detached = False
    if state.answer is not None:
        state.answer.state = "terminal"
        state.answer.answer_text = final_answer
    state.footer.state = "terminal"
    state.footer.ctx_used = ctx_used
    state.footer.elapsed_s = elapsed_s
    state.top_buttons = ()
```

- [ ] **Step 5.5: 测试通过 + commit**

Run: `pytest tests/test_unified_card_buttons.py tests/test_unified_card_detach_transition.py tests/test_unified_card_ph_replaced.py -v`
Expected: 7 PASS。

```bash
git add src/xana/agent_adapter/channels/unified_card.py
git commit -m "feat(card): top buttons + detach transition + ph_replaced terminal"
cd tests && git add test_unified_card_buttons.py test_unified_card_detach_transition.py test_unified_card_ph_replaced.py && \
  git commit -m "test(card): buttons + detach/ph_replaced transitions" && cd ..
```

---

## Task 6 — Menu section + attach 候选

**Files:**
- Modify: `src/xana/agent_adapter/channels/unified_card.py`
- Test: `tests/test_unified_card_menu_section.py`

### Step 6.1 — 写测试

```python
# tests/test_unified_card_menu_section.py
"""菜单卡:无 compact/answer,有 menu body + 底部按钮区。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, MenuSection, Button, ButtonRow,
    build_attach_menu, build_help_menu,
)


def test_help_menu_only_has_menu_section():
    state = build_help_menu(card_id="cmd-1",
                             specs=(("/help", "显示菜单"), ("/clear", "清空上下文")))
    assert state.menu is not None
    assert "/help" in state.menu.body_md
    assert state.compact is None
    assert state.answer is None
    # 命令按钮在底部
    assert len(state.bottom_buttons) >= 1


def test_attach_menu_lists_candidates_as_bottom_buttons():
    state = build_attach_menu(card_id="cmd-2",
                                candidates=(("run-a", "review xana"), ("run-b", "explain compact")))
    assert state.menu is not None
    btns = [b for row in state.bottom_buttons for b in row.buttons]
    sends = [b.send for b in btns]
    assert "/attach run-a" in sends
    assert "/attach run-b" in sends
```

- [ ] **Step 6.1: 写测试 + 跑确认失败**

Run: `pytest tests/test_unified_card_menu_section.py -v`

- [ ] **Step 6.2: 实现 builder**

```python
def build_help_menu(*, card_id: str, specs: tuple[tuple[str, str], ...]) -> UnifiedCardState:
    lines = ["可用命令:"]
    for name, desc in specs:
        lines.append(f"  {name:<20} {desc}")
    body = "\n".join(lines)
    sends = [Button(text=name, send=name) for name, _ in specs]
    rows = tuple(ButtonRow(buttons=tuple(sends[i:i+2])) for i in range(0, len(sends), 2))
    return UnifiedCardState(
        card_id=card_id, menu=MenuSection(body_md=body),
        bottom_buttons=rows,
    )


def build_attach_menu(*, card_id: str, candidates: tuple[tuple[str, str], ...]) -> UnifiedCardState:
    lines = ["可恢复的后台 run:"]
    btns = []
    for run_id, label in candidates:
        lines.append(f"  {run_id}  · {label[:30]}")
        btns.append(Button(text=f"切回 {label[:20]}", send=f"/attach {run_id}"))
    rows = tuple(ButtonRow(buttons=(b,)) for b in btns)
    return UnifiedCardState(
        card_id=card_id, menu=MenuSection(body_md="\n".join(lines)),
        bottom_buttons=rows,
    )
```

- [ ] **Step 6.3: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/channels/unified_card.py
git commit -m "feat(card): menu section builders (help + attach)"
cd tests && git add test_unified_card_menu_section.py && \
  git commit -m "test(card): menu section help/attach" && cd ..
```

---

## Task 7 — Feishu renderer

**Files:**
- Create: `src/xana/agent_adapter/channels/unified_card_feishu.py`
- Test: `tests/test_unified_card_feishu_render.py`

### Step 7.1 — 写测试(确保 schema 关键字段在)

```python
# tests/test_unified_card_feishu_render.py
"""飞书 renderer:输出合法 schema,包含期望 element 类型。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, ToolBlock,
    FooterText, MenuSection, Button, ButtonRow,
)
from xana.agent_adapter.channels.unified_card_feishu import render as render_feishu


def test_empty_state_minimum_schema():
    state = UnifiedCardState(card_id="c1",
                              footer=FooterText(model="m", elapsed_s=0, state="fg_running"))
    schema = render_feishu(state)
    assert schema["msg_type"] == "interactive" or "card" in schema or "elements" in schema.get("card", {})


def test_compact_running_renders_progress_bar():
    state = UnifiedCardState(card_id="c1",
                              compact=CompactSection(state="streaming", progress_cells=7),
                              footer=FooterText(model="m", state="fg_running"))
    schema = render_feishu(state)
    # 进度条以 ▰/▱ 表示,7 ▰ + 3 ▱
    txt = str(schema)
    assert "▰" in txt and "▱" in txt


def test_answer_streaming_renders_thinking_tools_answer():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="streaming", thinking_text="t",
                                                    tools=(ToolBlock(name="Read", header="Read /a", body_md="b"),),
                                                    answer_text="a"),
                              footer=FooterText(model="m", state="fg_running"))
    schema = render_feishu(state)
    txt = str(schema)
    assert "思考" in txt
    assert "Read /a" in txt
    assert "a" in txt  # answer text


def test_detached_renders_placeholder_for_answer():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="detached", answer_text="x"),
                              footer=FooterText(model="m", state="bg_frozen"),
                              detached=True)
    schema = render_feishu(state)
    txt = str(schema)
    assert "回答中" in txt or "正在后台" in txt
    assert "🟡 后台运行中" in txt


def test_top_buttons_rendered_in_header_position():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="streaming"),
                              top_buttons=(ButtonRow(buttons=(Button(text="⏹ 停止", send="/cancel x"),)),),
                              footer=FooterText(model="m", state="fg_running"))
    schema = render_feishu(state)
    txt = str(schema)
    assert "停止" in txt
```

- [ ] **Step 7.1: 写测试 + 跑确认失败**

- [ ] **Step 7.2: 实现 feishu renderer**

```python
# src/xana/agent_adapter/channels/unified_card_feishu.py
"""飞书卡 renderer:UnifiedCardState → 飞书 interactive card schema。

借用 _panel(collapsible_panel)等已有 helper。
"""
from __future__ import annotations

from typing import Any

from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, ToolBlock,
    ButtonRow, TOOLS_VISIBLE_LIMIT,
)


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _btn(b) -> dict:
    return {
        "tag": "button", "type": "default",
        "text": {"tag": "plain_text", "content": b.text},
        **({"value": {"send": b.send}} if b.send else {}),
    }


def _btn_row(row: ButtonRow) -> dict:
    return {"tag": "column_set", "columns": [
        {"tag": "column", "elements": [_btn(b)]}
        for b in row.buttons
    ]}


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _panel(title: str, body_md: str, *, folded: bool = True) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": not folded,
        "header": {"title": {"tag": "markdown", "content": title}},
        "elements": [_md(body_md or " ")],
    }


def _render_compact(sec: CompactSection) -> list[dict]:
    out: list[dict] = []
    if sec.state == "completed":
        title = "上下文压缩完成"
    elif sec.state == "cancelled":
        title = "压缩已取消"
    else:
        title = "正在压缩上下文"
    out.append(_md(f"**{title}**"))
    if sec.state != "cancelled":
        out.append(_md(f"`{_progress_bar(sec.progress_cells)}`"))
    panel_title = (
        f"已压缩 {sec.compacted_n} 条消息" if sec.state == "completed"
        else "压缩已取消" if sec.state == "cancelled"
        else "压缩中"
    )
    out.append(_panel(panel_title, sec.summary_text or " ", folded=True))
    return out


def _render_answer(sec: AnswerSection) -> list[dict]:
    out: list[dict] = []
    # thinking 块
    think_title = "思考过程" if sec.thinking_text else "正在思考"
    out.append(_panel(think_title, sec.thinking_text or " ",
                       folded=(sec.state != "streaming")))
    # tools 块
    visible_tools = sec.tools[:TOOLS_VISIBLE_LIMIT] if sec.state in ("detached", "terminal") else sec.tools
    for t in visible_tools:
        out.append(_panel(t.header, t.body_md or " ", folded=True))
    # tools 提示块
    if sec.tools_pending > 0:
        out.append(_md(f"**+{sec.tools_pending} 待加载工具**"))
        out.append(_btn_row(ButtonRow(buttons=())))  # 占位:加载按钮由 adapter 侧装填
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        out.append(_md("_正在组织回答_"))
    # 回答块
    if sec.state == "detached":
        out.append(_panel("回答中…", "_回答仍在后台生成,完成后会自动更新_", folded=True))
    elif sec.state == "terminal":
        out.append(_panel("回答", sec.answer_text or " ", folded=True))
    elif sec.state == "cancelled":
        out.append(_md(f"**回答已取消**\n\n{sec.answer_text}"))
    else:
        # streaming
        out.append(_md(f"**回答**\n\n{sec.answer_text or '_…_'}"))
    return out


def render(state: UnifiedCardState) -> dict:
    elements: list[dict] = []
    # 顶部按钮
    for row in state.top_buttons:
        elements.append(_btn_row(row))
    if state.compact is not None:
        elements.extend(_render_compact(state.compact))
    if state.answer is not None:
        elements.extend(_render_answer(state.answer))
    if state.menu is not None:
        elements.append(_md(state.menu.body_md))
    # 底部按钮
    for row in state.bottom_buttons:
        elements.append(_btn_row(row))
    # 脚标
    elements.append(_md(f"_{state.footer.render()}_"))
    return {"msg_type": "interactive", "card": {"elements": elements}}
```

- [ ] **Step 7.3: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/channels/unified_card_feishu.py
git commit -m "feat(card-feishu): renderer for UnifiedCardState"
cd tests && git add test_unified_card_feishu_render.py && \
  git commit -m "test(card-feishu): render schema for compact/answer/detach/buttons" && cd ..
```

---

## Task 8 — Telegram renderer

**Files:**
- Create: `src/xana/agent_adapter/channels/unified_card_telegram.py`
- Test: `tests/test_unified_card_telegram_render.py`

### Step 8.1 — 写测试

```python
# tests/test_unified_card_telegram_render.py
"""TG renderer:返回 (html, inline_keyboard)。所有按钮入 inline_keyboard 底部。"""
from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, ToolBlock,
    FooterText, MenuSection, Button, ButtonRow,
)
from xana.agent_adapter.channels.unified_card_telegram import render as render_tg


def test_empty_state_just_footer():
    state = UnifiedCardState(card_id="c1",
                              footer=FooterText(model="m", elapsed_s=5, state="fg_running"))
    html, kb = render_tg(state)
    assert "m" in html
    assert "5s" in html
    assert kb == []


def test_compact_streaming_includes_progress_bar():
    state = UnifiedCardState(card_id="c1",
                              compact=CompactSection(state="streaming", progress_cells=5),
                              footer=FooterText(model="m", state="fg_running"))
    html, kb = render_tg(state)
    assert "▰" in html and "▱" in html


def test_detached_renders_placeholder_block():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="detached", answer_text="x"),
                              footer=FooterText(model="m", state="bg_frozen"),
                              detached=True)
    html, kb = render_tg(state)
    assert "回答中" in html or "后台" in html
    assert "🟡 后台运行中" in html


def test_buttons_all_go_to_inline_keyboard_bottom():
    state = UnifiedCardState(card_id="c1",
                              answer=AnswerSection(state="streaming"),
                              top_buttons=(ButtonRow(buttons=(Button(text="⏹ 停止", send="/cancel x"),)),),
                              bottom_buttons=(ButtonRow(buttons=(Button(text="more", send="/x"),)),),
                              footer=FooterText(model="m", state="fg_running"))
    html, kb = render_tg(state)
    # TG 把顶部 + 底部都堆 inline_keyboard;顺序:top 先 bottom 后
    assert len(kb) == 2
    assert kb[0][0]["text"] == "⏹ 停止"
    assert kb[1][0]["text"] == "more"
```

- [ ] **Step 8.1: 写测试 + 跑确认失败**

- [ ] **Step 8.2: 实现 TG renderer**

```python
# src/xana/agent_adapter/channels/unified_card_telegram.py
"""Telegram HTML + inline_keyboard renderer for UnifiedCardState。

TG inline_keyboard 总在底部,所以 top_buttons + bottom_buttons 都堆进 kb,
top 在前 bottom 在后。HTML 用 <blockquote expandable> 表达折叠区。
"""
from __future__ import annotations

from xana.agent_adapter.channels.unified_card import (
    UnifiedCardState, CompactSection, AnswerSection, TOOLS_VISIBLE_LIMIT,
)


def _bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _fold(title: str, body: str) -> str:
    body = body or " "
    return f"<blockquote expandable><b>{title}</b>\n{body}</blockquote>"


def _render_compact(sec: CompactSection) -> str:
    if sec.state == "completed":
        title = "上下文压缩完成"
    elif sec.state == "cancelled":
        title = "压缩已取消"
    else:
        title = "正在压缩上下文"
    out = [f"<b>{title}</b>"]
    if sec.state != "cancelled":
        out.append(f"<code>{_bar(sec.progress_cells)}</code>")
    panel_title = (
        f"已压缩 {sec.compacted_n} 条消息" if sec.state == "completed"
        else "压缩已取消" if sec.state == "cancelled"
        else "压缩中"
    )
    out.append(_fold(panel_title, sec.summary_text or " "))
    return "\n".join(out)


def _render_answer(sec: AnswerSection) -> str:
    out: list[str] = []
    think_title = "思考过程" if sec.thinking_text else "正在思考"
    out.append(_fold(think_title, sec.thinking_text or " "))
    visible = sec.tools[:TOOLS_VISIBLE_LIMIT] if sec.state in ("detached", "terminal") else sec.tools
    for t in visible:
        out.append(_fold(t.header, t.body_md or " "))
    if sec.tools_pending > 0:
        out.append(f"<b>+{sec.tools_pending} 待加载工具</b>")
    elif sec.state == "streaming" and not sec.tools and not sec.answer_text:
        out.append("<i>正在组织回答</i>")
    if sec.state == "detached":
        out.append(_fold("回答中…", "<i>回答仍在后台生成,完成后会自动更新</i>"))
    elif sec.state == "terminal":
        out.append(_fold("回答", sec.answer_text or " "))
    elif sec.state == "cancelled":
        out.append(f"<b>回答已取消</b>\n\n{sec.answer_text}")
    else:
        out.append(f"<b>回答</b>\n\n{sec.answer_text or '<i>…</i>'}")
    return "\n".join(out)


def _kb_row(row) -> list[dict]:
    out = []
    for b in row.buttons:
        item: dict = {"text": b.text}
        if b.send:
            item["callback_data"] = b.send
        out.append(item)
    return out


def render(state: UnifiedCardState) -> tuple[str, list[list[dict]]]:
    parts: list[str] = []
    if state.compact is not None:
        parts.append(_render_compact(state.compact))
    if state.answer is not None:
        parts.append(_render_answer(state.answer))
    if state.menu is not None:
        parts.append(state.menu.body_md)
    parts.append(f"<i>{state.footer.render()}</i>")
    html = "\n\n".join(parts)
    kb: list[list[dict]] = []
    for row in state.top_buttons:
        kb.append(_kb_row(row))
    for row in state.bottom_buttons:
        kb.append(_kb_row(row))
    return html, kb
```

- [ ] **Step 8.3: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/channels/unified_card_telegram.py
git commit -m "feat(card-tg): HTML + inline_keyboard renderer"
cd tests && git add test_unified_card_telegram_render.py && \
  git commit -m "test(card-tg): renderer schema for compact/answer/detach/buttons" && cd ..
```

---

## Task 9 — ProgressEvent kind 扩 + commands.py 串新事件

**Files:**
- Modify: `src/xana/agent_adapter/contracts.py`(若有 kind 枚举)
- Modify: `src/xana/agent_adapter/commands.py`(`/help` / `/attach` / `_cmd_compact` 发新 kind)
- Test: `tests/test_unified_card_events_wiring.py`

(此 task 触碰 contracts.py,需 user 在 Task 0 已批。)

### Step 9.1 — 加事件 kind 字符串

`contracts.py` 若 ProgressEvent 用 free-form `kind: str` 则不动;若有 Literal,扩为:
```python
ProgressEventKind = Literal[
    "run_started", "model_delta", "tool_started", "tool_finished",
    "compact_progress", "run_detached", "ph_replaced",
    "cancelled", "final", "error",
    # 新增 ⬇
    "compact_started", "menu_response", "attach_options",
]
```

### Step 9.2 — `_cmd_compact` 发 `compact_started` 而非直接走老卡

把 `_cmd_compact` 里 `await self.progress_sink.report(ProgressEvent(...kind="compact_progress"...))` 之前加一条 `compact_started`:
```python
await self.progress_sink.report(ProgressEvent(
    session_id=inbound.session_id, run_id=run_id,
    message="compact_started",
    payload={"kind": "compact_started", "session_id": inbound.session_id,
             "run_id": run_id, "model": model},
))
```

### Step 9.3 — `_cmd_help` 发 `menu_response`(替代当前 buttons payload)

将现有 `_cmd_help` 返回的 `CommandResult` 改为附带 `payload["kind"]="menu_response"`,adapter 端识别后用 `build_help_menu` 构造卡。

### Step 9.4 — 加 `_cmd_attach` 命令(若已有则改 payload kind 为 `attach_options`)

(若 `_cmd_attach` 已存在,只确认它产出 `kind="attach_options"` 的 payload。)

### Step 9.5 — 写测试 + 测试通过

```python
# tests/test_unified_card_events_wiring.py
"""commands 路径发出新 kind 事件。"""
import pytest
from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.contracts import InboundMessage


class _SpySink:
    def __init__(self): self.events = []
    async def report(self, e): self.events.append(e)


@pytest.mark.asyncio
async def test_help_emits_menu_response_payload():
    sink = _SpySink()
    router = DefaultCommandRouter(progress_sink=sink)
    result = await router.route_command(InboundMessage(
        adapter_id="cli", session_id="s1", text="/help"))
    # /help 返回 CommandResult,payload kind 为 menu_response 之一(adapter 据此选 builder)
    assert result.payload.get("kind") == "menu_response" or "menu" in str(result.payload)
```

- [ ] **Step 9.1: 改 contracts.py**

- [ ] **Step 9.2: 改 _cmd_compact**

- [ ] **Step 9.3: 改 _cmd_help**

- [ ] **Step 9.4: 检 _cmd_attach**

- [ ] **Step 9.5: 跑测试 + commit**

```bash
git add src/xana/agent_adapter/contracts.py src/xana/agent_adapter/commands.py
git commit -m "feat(commands): emit compact_started/menu_response/attach_options events"
cd tests && git add test_unified_card_events_wiring.py && \
  git commit -m "test(commands): new event kinds emitted" && cd ..
```

---

## Task 10 — Feishu adapter 切到 UnifiedCardState + 删旧 builder

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu.py`(替换 render path)
- Delete from: `src/xana/agent_adapter/channels/feishu_card.py`(删 `build_fg_streaming_card` / `build_bg_placeholder_card`)
- Modify tests:删/改对应 test_feishu_render_turn / test_feishu_dual_card

### Step 10.1 — 在 feishu.py 加 `_card_states: dict[str, UnifiedCardState]` 注册

在 `FeishuAdapter.__init__`:
```python
from xana.agent_adapter.channels.unified_card import UnifiedCardState
from xana.agent_adapter.channels.unified_card_feishu import render as _render
self._card_states: dict[str, UnifiedCardState] = {}  # card_id -> state
self._render_unified = _render
```

### Step 10.2 — 改 `render(event)`:按 event.kind 派发到 state patcher

```python
async def render(self, event: AgentEvent) -> RenderedMessage:
    state = self._get_or_create_state(event)
    self._patch_state(state, event)
    schema = self._render_unified(state)
    if state.message_id is None:
        # 首次发卡
        message_id = await self._send_card(state.card_id, schema)
        state.message_id = message_id
    else:
        await self._patch_card(state.message_id, schema)
    return RenderedMessage(adapter_id="feishu", session_id=event.session_id, ...)
```

`_patch_state` 是大开关:`run_started` → 新建 state + answer section;`compact_started` → 加 compact section;`model_delta` → 推 thinking/answer;`tool_started/finished` → append_tool;`compact_progress` → advance_compact_*;`run_detached` → apply_detach;`ph_replaced` → apply_ph_replaced;`menu_response` → build_help_menu;`attach_options` → build_attach_menu。

### Step 10.3 — 从 `feishu_card.py` 删 `build_fg_streaming_card` / `build_bg_placeholder_card` 及只用一次的辅助函数

保留 `_panel` / `_tool_panel`(unified_card_feishu 复用)。

### Step 10.4 — 同 PR 删老测试 / 改保留:
- `tests/test_feishu_render_turn.py` — 改成测 UnifiedCardState 渲染(若已大改可直接删,新 feishu_render 测试覆盖)
- `tests/test_feishu_dual_card.py` — 改成测新 state 双卡场景

### Step 10.5 — 跑回归 + commit

Run: `pytest tests/test_unified_card_*.py tests/test_feishu_*.py -v`
Expected:新测试全过,老测试已删或改。

```bash
git add src/xana/agent_adapter/channels/feishu.py src/xana/agent_adapter/channels/feishu_card.py
git commit -m "feat(feishu): use UnifiedCardState; drop old fg_streaming/bg_placeholder builders"
cd tests && git add -A test_feishu_*.py && \
  git commit -m "test(feishu): adapt to UnifiedCardState renderer" && cd ..
```

---

## Task 11 — Telegram adapter 切到 UnifiedCardState + 删旧

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram.py`
- Delete from: `src/xana/agent_adapter/channels/telegram_html.py`(同等价路径)
- Tests:同步调整

镜像 Task 10。每 step 都做对应 TG 改动。

- [ ] **Step 11.1: TG adapter 加 _card_states + render_unified 注入**
- [ ] **Step 11.2: 改 render(event) 派发到 state patcher**
- [ ] **Step 11.3: 删 telegram_html.py 旧 builder**
- [ ] **Step 11.4: 改/删老 TG 测试**
- [ ] **Step 11.5: 跑回归 + commit**

```bash
git add src/xana/agent_adapter/channels/telegram.py src/xana/agent_adapter/channels/telegram_html.py
git commit -m "feat(telegram): use UnifiedCardState; drop old html builders"
cd tests && git add -A test_telegram_*.py && \
  git commit -m "test(telegram): adapt to UnifiedCardState renderer" && cd ..
```

---

## Task 12 — Detach 时序锁(compact 不许在 run_detached 前发 compact_started)

**File:**
- Test: `tests/test_unified_card_detach_then_compact.py`

确认 `/compact` 触发时 `run_detached`(给被打断的前台 run)**先于** `compact_started` 发出 → adapter 端先 patch 旧 fg 卡 detach 再创建 compact 段。

- [ ] **Step 12.1: 写时序测试**

```python
"""/compact 期间被打断的前台 run 的 run_detached 必须先于 compact_started 发出。"""
# 用 SpySink 收集事件,断言索引(idx_run_detached < idx_compact_started)
```

- [ ] **Step 12.2: 跑确认通过 + commit**

(若失败,在 commands._cmd_compact 里调整事件发射顺序)

---

## Task 13 — Sync + e2e

- [ ] **Step 13.1: 跑全套 UnifiedCard + adapter 测试**

```bash
pytest tests/test_unified_card_*.py tests/test_feishu_*.py tests/test_telegram_*.py -v
```
Expected: ALL PASS。

- [ ] **Step 13.2: 跑全仓回归**

`pytest tests/ -v -x`

任何回归先停定位。

- [ ] **Step 13.3: `xana sync`**

`~/.xana/bin/xana sync`(自动重启 serve)

- [ ] **Step 13.4: 人工 e2e**(写进 PR 描述)

1. 飞书发"你好" → 卡片只有回答段,完成后脚标显示用时
2. 发长任务 → 流式渲染,顶部 ⏹/➡
3. 长任务中点 ➡ 后台 → 卡片折叠为后台冻结态;脚标 🟡 后台运行中;按钮消失
4. 长任务中发 `/compact` → 当前 fg run detach + 当前卡 compact 段插入(无 compact 取消按钮)
5. 后台 run 完成 → 一次性 patch 终态,无渲染抖动
6. `/help` → 菜单卡,底部命令按钮
7. `/attach` → 菜单卡列后台候选,底部 attach 按钮
8. tg 同步走一遍 1-7

- [ ] **Step 13.5: 写 PR**

PR 标题:`feat(card): unified PH card model (replaces fg_streaming/bg_placeholder)`
PR body 引 spec + plan 路径,贴 Step 13.4 五步对比截图(旧 vs 新)。

---

## 不在本 plan 范围

- CLI Textual REPL 的 UI 适配:走"CLI 整包"。
- `generate_summary` 加 prefill/output 计时日志:独立 task,本 spec 提了需求但不实施。
- P6 compact 算法本身(fork_point / 子分支 compact 等):见 P6 plan。

---

## Self-Review

**Spec coverage(`2026-06-15-unified-ph-card-design.md`):**
- §2 骨架 → Task 1/7/8 ✓
- §3 出现规则 → Task 9 `compact_started`、Task 10/11 state patcher 派发 ✓
- §4.1 compact 进度条 → Task 3 ✓
- §4.2 回答段状态 → Task 4/5 ✓
- §4.3 tools 块溢出 → Task 4 ✓
- §5 顶部按钮 → Task 5 ✓
- §6 底部按钮(菜单/attach)→ Task 6 ✓
- §7 脚标三态 → Task 2 ✓
- §8 事件契约 → Task 9 ✓
- §9 PH 顺序不变量(只 edit 不 recreate)→ Task 10/11 状态字典持有 message_id ✓
- §10 数据模型 → Task 1 ✓
- §11 清理 → Task 10/11 ✓

**Placeholder scan:** 无 TBD / TODO。`_patch_state` 的具体派发表(Task 10 Step 10.2)写成 prose 而非代码 — 实施期按 spec §8 事件契约展开,每条 kind 一个 if 分支调对应 unified_card 函数。

**Type consistency:**
- `CompactSection.state` 用 `Literal["input","streaming","completed","cancelled"]`(全 task 一致)
- `AnswerSection.state` 用 `Literal["streaming","detached","terminal","cancelled"]`(全 task 一致)
- `FooterText.state` 用 `Literal["fg_running","bg_frozen","terminal"]`
- `TOOLS_VISIBLE_LIMIT = 3` 在 unified_card / feishu / tg renderer 共享 import
- Button.send 类型 `str | None`;ButtonRow.buttons `tuple[Button, ...]`

**接口审批门**(`feedback_interfaces_locked.md`):
- Task 0:`contracts.py` ProgressEvent kind 扩 `compact_started` / `menu_response` / `attach_options`(若是 Literal)。
- Task 0:`interfaces.py` Adapter ABC 不动(优选方案 — 在 feishu/telegram 内部走 unified path,ABC 保持向下兼容)。
- 实施期 Task 0 必须先与用户确认,通过才开 Task 1。
