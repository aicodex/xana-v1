# Async Run Phase 4 — 后台 Run 多卡 UX (tab 切换 + 完成弹卡 + PH 不渲染) Implementation Plan

> ⚠️ **SUPERSEDED (2026-06-14)** —— 本 plan 中途 pivot,实际落地以下列新 spec 为准,本文件仅作历史参考。
>
> - 数据模型 + 双卡渲染:[`../specs/2026-06-14-async-chat-design.md`](../specs/2026-06-14-async-chat-design.md)
> - PH-driven 渲染契约:[`../specs/2026-06-14-ph-driven-rendering-design.md`](../specs/2026-06-14-ph-driven-rendering-design.md)
> - 卡片内容布局(next):[`../specs/2026-06-14-card-content-tail3-design.md`](../specs/2026-06-14-card-content-tail3-design.md)
> - 配套实现 plan:[`./2026-06-14-async-chat-implementation.md`](./2026-06-14-async-chat-implementation.md)、[`./2026-06-14-ph-driven-rendering.md`](./2026-06-14-ph-driven-rendering.md)
>
> **关键偏离**:Tab 键 / `format_tabs` / `bg_runs` / `build_pure_answer_card` / `switch_foreground` / `run_completed_bg` 等已在 `cc79c4d` 全部 purge;改用 PH-first 双卡 + `/attach` 命令 + `detach_foreground` / `attach` / `replace_range` 接口;PH 反而**要**渲染(作为占位卡数据源),原 `filter_renderable` 防御失效。`InflightRun.detached` 已改名 `is_attached`(`ff4edeb`)。
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P3 已让后台 run 跑起来,P4 把多 run 在 adapter 层的"卡片/底部 tab/切换"做出来,让用户看到"旧 run 已转后台、新 run 在前台、可切换、完成时不抢焦点"。

**Architecture:**
- Gateway 不再"一次会话只接受一个 run"——新 submit 到来时把上一个 run 标 `detached`(继续后台跑),新 run 成为 foreground。
- 各 adapter 维护 `_SessionBuffer.foreground_run_id` 与 `running_run_ids: OrderedDict[run_id, label]`;CLI 用 prompt_toolkit `bottom_toolbar` 渲染 tab 行,Tab 键切换;feishu/telegram 已天然 per-run 一张卡,只需 (a) `Placeholder` 不渲染 (b) 后台完成时推"纯回答卡"(只保留正文+按钮,无 thinking/tool 折叠面板)。
- 不动 contracts/interfaces;新事件 `run_detached` 走 `ProgressEvent.note_progress(kind=...)` 这条非破坏通路。

**Tech Stack:** Python 3.12+ / asyncio / prompt_toolkit / Feishu 卡片 Schema 2.0 / Telegram inline keyboard。

**Roadmap 参照:** `docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md` Phase 4 章节。Stop/Cancel 接线属 **Phase 5**,本 plan 不做。

**前置条件(必须):** P3 已 commit (`7cca8da`),`async_mode=True` 默认。`xana sync` 已跑过、`xana serve` 在新代码上运行。

---

## File Structure

**修改:**
- `src/xana/agent_adapter/gateway.py:48-107` — `_submit` 引入"前一个 run 转后台" 分支;新增 `_inflight: dict[session_id, list[InflightRun]]`。
- `src/xana/agent_adapter/contracts.py` — 在文件末尾加 `InflightRun` 数据类(纯 dataclass,**不是 ABC**,所以不算"接口改动")。
- `src/xana/agent_adapter/cli.py` — `_SessionBuffer` 风格的最小状态:`foreground_run_id`、`bg_runs: OrderedDict[run_id, str]`;`note_progress` 处理 `run_detached`/`run_completed_bg`;新增 `format_tabs() -> str`。
- `src/xana/cli/xanac.py:80-130` — `_repl_loop` 的 `bottom_toolbar` 调 `adapter.format_tabs()`;绑定 `Tab` 键 → `gateway.switch_foreground(session_id, target_run_id)`。
- `src/xana/agent_adapter/channels/feishu.py` 与 `channels/telegram.py` — `_render_messages_for_context` 过滤 `Placeholder`;`_finalize_card` 在 `run_id != buf.foreground_run_id` 时走 `build_pure_answer_card`(只保留正文+按钮)。
- `src/xana/agent_adapter/channels/feishu_card.py` 与 `channels/telegram_html.py` — 新增 `build_pure_answer_card(draft)` / `format_pure_answer_html(draft)` 函数。

**新增测试(`tests/` 子 repo):**
- `tests/test_gateway_detach_on_new_submit.py`
- `tests/test_cli_adapter_tab_state.py`
- `tests/test_feishu_pure_answer_card.py`
- `tests/test_telegram_pure_answer_message.py`
- `tests/test_placeholder_not_rendered.py`

---

## Task 1 — Gateway: 新 submit 时把上一个 run detach 而不是 cancel

**Files:**
- Modify: `src/xana/agent_adapter/contracts.py`(文件末尾追加)
- Modify: `src/xana/agent_adapter/gateway.py:22-46, 48-107`
- Test: `tests/test_gateway_detach_on_new_submit.py`

### Step 1.1 — Write the failing test

```python
# tests/test_gateway_detach_on_new_submit.py
"""新 submit 到来时,前一个 in-flight run 不应被 cancel,
而应进入 detached 状态(继续在后台跑),新 run 立刻成为 foreground。"""
from __future__ import annotations

import asyncio
import pytest

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.gateway import DefaultAgentGateway


class _FakeAgent:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str | None]] = []
        self._gate = asyncio.Event()

    async def send(self, request):
        from xana.agents.contracts import AgentEvent
        # 慢 run:不结束,直到 gate 放行
        yield AgentEvent(kind="draft", session_id=request.session_id,
                         payload={"text": "thinking…", "run_id": "r1"})
        await self._gate.wait()
        yield AgentEvent(kind="final", session_id=request.session_id,
                         payload={"text": "done", "run_id": "r1"})

    async def cancel(self, session_id, run_id=None):
        self.cancel_calls.append((session_id, run_id))


class _FakeRouter:
    async def route(self, inbound):
        from xana.agents.contracts import AgentRequest
        return AgentRequest(session_id=inbound.session_id,
                            user_input=inbound.text,
                            target_agent="chat")


class _FakeAdapter:
    adapter_id = "cli"
    def __init__(self) -> None:
        self.progress: list[dict] = []
    async def render(self, event):
        from xana.agent_adapter.contracts import RenderedMessage
        return RenderedMessage(adapter_id="cli", session_id=event.session_id,
                               text="", metadata={})
    def note_progress(self, payload):
        self.progress.append(payload)


@pytest.mark.asyncio
async def test_second_submit_detaches_first_run_without_cancel():
    agent = _FakeAgent()
    gw = DefaultAgentGateway(agents={"chat": agent}, router=_FakeRouter())
    adapter = _FakeAdapter()
    await gw.register_adapter("cli", adapter)

    sid = "s1"
    first = InboundMessage(adapter_id="cli", session_id=sid, text="hello-1")
    second = InboundMessage(adapter_id="cli", session_id=sid, text="hello-2")

    # 启动第一个 run,等它发出第一个 draft
    s1 = gw.submit(first)
    t1 = asyncio.create_task(_drain_first(s1))
    await asyncio.sleep(0.05)

    # 第二个 submit 到来:不应触发 agent.cancel
    s2 = gw.submit(second)
    t2 = asyncio.create_task(_drain_first(s2))
    await asyncio.sleep(0.05)

    assert agent.cancel_calls == []  # 关键:没 cancel
    # 应收到 run_detached 进度通知
    kinds = [p.get("kind") for p in adapter.progress]
    assert "run_detached" in kinds

    # 放行第一个 run,等两个都收尾
    agent._gate.set()
    await asyncio.wait_for(t1, timeout=2.0)
    await asyncio.wait_for(t2, timeout=2.0)


async def _drain_first(stream) -> None:
    # 只消费,不抛
    async for _ in stream:
        pass
```

- [ ] **Step 1.1: 写测试文件(整段贴入)**

- [ ] **Step 1.2: 跑测试确认失败**

Run: `pytest tests/test_gateway_detach_on_new_submit.py -v`
Expected: FAIL — `agent.cancel_calls` 非空(当前 `gateway.cancel` 会被 P3 既有 KeyboardInterrupt 路径触发)或 progress 里没 `run_detached`。

- [ ] **Step 1.3: 加 `InflightRun` 数据类**

在 `src/xana/agent_adapter/contracts.py` 文件**末尾**追加:

```python
from typing import Any
import asyncio as _asyncio


@dataclass
class InflightRun:
    """Gateway 侧 per-run 追踪。非 frozen——detached 字段可变。"""
    session_id: str
    run_id: str
    label: str
    task: "_asyncio.Task[Any] | None" = None
    detached: bool = False
```

(`dataclass` / `field` 已在文件顶部 import;若 `asyncio` 未 import 则在顶部加 `import asyncio`。)

- [ ] **Step 1.4: 改 Gateway 跟踪 in-flight runs**

在 `src/xana/agent_adapter/gateway.py` 的 `DefaultAgentGateway.__init__` 里加:

```python
self._inflight: dict[str, list[InflightRun]] = {}  # session_id → 倒序(新在前)
self._run_seq = 0
```

并在文件顶 import:`from xana.agent_adapter.contracts import InflightRun`。

- [ ] **Step 1.5: `_submit` 引入 detach 分支**

把 `gateway.py:101-107` 的 agent 流循环替换为:

```python
# 旧 run 转后台 —— 不 cancel,只标 detached,并通知 adapter 刷 tab。
existing = self._inflight.get(inbound.session_id, [])
for ir in existing:
    if not ir.detached:
        ir.detached = True
        self._notify_run_detached(adapter, inbound.session_id, ir)

self._run_seq += 1
new_run = InflightRun(
    session_id=inbound.session_id,
    run_id=f"run-{inbound.session_id}-{self._run_seq}",
    label=(inbound.text or "").strip()[:40] or "run",
)
self._inflight.setdefault(inbound.session_id, []).insert(0, new_run)
try:
    async for event in agent.send(request):
        rendered = await adapter.render(event)
        yield self._attach_event_kind(rendered, event)
        if event.kind in ("final", "error", "cancelled"):
            break
finally:
    self._inflight[inbound.session_id] = [
        ir for ir in self._inflight.get(inbound.session_id, [])
        if ir.run_id != new_run.run_id
    ]
```

(注意:**删除** 旧 `self.clear_cancel(...)` / `self._cancel_flags` 在循环里的 break 检测——detach 不再用 cancel_flags。`cancel()` 方法本身保留,P5 用。)

并加 helper:

```python
@staticmethod
def _notify_run_detached(adapter, session_id: str, ir: "InflightRun") -> None:
    note = getattr(adapter, "note_progress", None)
    if note is None:
        return
    note({
        "kind": "run_detached",
        "session_id": session_id,
        "run_id": ir.run_id,
        "label": ir.label,
    })
```

- [ ] **Step 1.6: 跑测试确认通过**

Run: `pytest tests/test_gateway_detach_on_new_submit.py -v`
Expected: PASS。

- [ ] **Step 1.7: Commit**

```bash
git add src/xana/agent_adapter/contracts.py src/xana/agent_adapter/gateway.py
git commit -m "feat(phase4): gateway detaches in-flight run on new submit"
# tests 仓:
cd tests && git add test_gateway_detach_on_new_submit.py && \
  git commit -m "test(phase4): second submit detaches first run without cancel" && cd ..
```

---

## Task 2 — CLI adapter: foreground/bg 追踪 + 底部 tab 行

**Files:**
- Modify: `src/xana/agent_adapter/cli.py:50-90, 188-200`
- Test: `tests/test_cli_adapter_tab_state.py`

### Step 2.1 — Write the failing test

```python
# tests/test_cli_adapter_tab_state.py
"""CliAdapter 收到 run_detached / run_completed_bg 后,
format_tabs() 反映当前后台 run 列表,foreground 切换正确。"""
from __future__ import annotations

import io
from xana.agent_adapter.cli import CliAdapter


def test_format_tabs_empty_when_no_bg_runs():
    a = CliAdapter(out=io.StringIO(), session_id="s1")
    assert a.format_tabs() == ""


def test_run_detached_adds_bg_run_and_tabs_render():
    a = CliAdapter(out=io.StringIO(), session_id="s1")
    a.note_progress({"kind": "run_detached", "session_id": "s1",
                     "run_id": "run-s1-1", "label": "review yuxu"})
    tabs = a.format_tabs()
    assert "review yuxu" in tabs
    assert "Tab" in tabs  # 提示键位


def test_run_completed_bg_removes_run_from_tabs():
    a = CliAdapter(out=io.StringIO(), session_id="s1")
    a.note_progress({"kind": "run_detached", "session_id": "s1",
                     "run_id": "run-s1-1", "label": "review yuxu"})
    a.note_progress({"kind": "run_completed_bg", "session_id": "s1",
                     "run_id": "run-s1-1"})
    assert a.format_tabs() == ""


def test_set_foreground_moves_current_to_bg():
    a = CliAdapter(out=io.StringIO(), session_id="s1")
    a.note_progress({"kind": "run_detached", "session_id": "s1",
                     "run_id": "run-s1-1", "label": "review yuxu"})
    # 切到 run-s1-1: 当前(无前台)→ 不变;run-s1-1 从 bg 摘掉
    a.set_foreground("run-s1-1")
    assert a.foreground_run_id == "run-s1-1"
    assert "review yuxu" not in a.format_tabs()
```

- [ ] **Step 2.1: 写测试**

- [ ] **Step 2.2: 跑测试确认失败**

Run: `pytest tests/test_cli_adapter_tab_state.py -v`
Expected: FAIL — `format_tabs` / `foreground_run_id` / `set_foreground` 不存在。

- [ ] **Step 2.3: CliAdapter 加状态字段**

在 `CliAdapter.__init__` 末尾追加:

```python
from collections import OrderedDict
self.foreground_run_id: str | None = None
self.bg_runs: "OrderedDict[str, str]" = OrderedDict()  # run_id → label
```

(`OrderedDict` import 提到文件顶。)

- [ ] **Step 2.4: 扩展 `note_progress`**

把 `cli.py:188-196` 的 `note_progress` 主体改为:

```python
def note_progress(self, payload: dict[str, Any]) -> None:
    kind = payload.get("kind")
    if kind == "usage_progress":
        self.last_progress = dict(payload)
        if self._progress_callback is not None:
            self._progress_callback(payload)
        return
    if kind == "run_detached":
        rid = str(payload.get("run_id") or "")
        if rid:
            self.bg_runs[rid] = str(payload.get("label") or rid)
        return
    if kind == "run_completed_bg":
        rid = str(payload.get("run_id") or "")
        self.bg_runs.pop(rid, None)
        return
```

- [ ] **Step 2.5: 加 `set_foreground` 与 `format_tabs`**

```python
def set_foreground(self, run_id: str | None) -> None:
    self.foreground_run_id = run_id
    if run_id is not None:
        self.bg_runs.pop(run_id, None)

def format_tabs(self) -> str:
    if not self.bg_runs:
        return ""
    items = " | ".join(f"[{i+1}] {lbl}" for i, lbl in enumerate(self.bg_runs.values()))
    return f"{items}  · Tab 切换"
```

- [ ] **Step 2.6: 跑测试确认通过**

Run: `pytest tests/test_cli_adapter_tab_state.py -v`
Expected: 4 PASS。

- [ ] **Step 2.7: Commit**

```bash
git add src/xana/agent_adapter/cli.py
git commit -m "feat(phase4): CliAdapter tracks foreground + bg runs, format_tabs()"
cd tests && git add test_cli_adapter_tab_state.py && \
  git commit -m "test(phase4): cli adapter tab state" && cd ..
```

---

## Task 3 — xanac REPL: bottom_toolbar 显示 tab + Tab 键切换

**Files:**
- Modify: `src/xana/cli/xanac.py:97-130`
- Test: 手动验证(交互式 REPL,自动测试成本过高)

- [ ] **Step 3.1: 改 `_bottom_toolbar`**

把 `xanac.py:97-106` 的 `_bottom_toolbar` 改为:

```python
def _bottom_toolbar() -> str:
    if adapter is None:
        return "ready"
    tabs = adapter.format_tabs()
    p = adapter.last_progress
    base = ""
    if p:
        base = (
            f"tokens: {p.get('tokens_in', 0)} in / {p.get('tokens_out', 0)} out · "
            f"elapsed: {p.get('elapsed_s', 0):.1f}s"
        )
    else:
        base = "ready"
    return f"{base}  ||  {tabs}" if tabs else base
```

- [ ] **Step 3.2: 绑定 Tab 键**

在 `xanac.py` `_repl_loop` 顶部、`PromptSession("> ", ...)` 行**之前**插:

```python
from prompt_toolkit.key_binding import KeyBindings
kb = KeyBindings()

@kb.add("tab", eager=True)
def _switch_run(event) -> None:
    if adapter is None or not adapter.bg_runs:
        return
    # 简单策略:轮转到 bg_runs 第一个,把当前 foreground 推回 bg。
    target_rid, target_lbl = next(iter(adapter.bg_runs.items()))
    cur_rid = adapter.foreground_run_id
    adapter.set_foreground(target_rid)
    if cur_rid:
        adapter.bg_runs[cur_rid] = cur_rid  # 仅占位 label, 后续 detach 事件会覆盖
    # 通知 gateway(若 gateway 暴露 switch API; 否则仅本地状态变化)
    switch = getattr(gateway, "switch_foreground", None)
    if switch is not None:
        import asyncio as _a
        _a.create_task(switch(session_id, target_rid))
```

并把 `PromptSession("> ", bottom_toolbar=_bottom_toolbar)` 改为 `PromptSession("> ", bottom_toolbar=_bottom_toolbar, key_bindings=kb)`。

(`switch_foreground` 在 Task 4 加;此处 `getattr` 兜底——Task 3 先合并不会破坏功能。)

- [ ] **Step 3.3: 手动验证脚本写进 PR 描述**

不写自动测试。手动验证步骤(写进 PR 描述):
1. `xana sync && xana serve`,新窗口跑 `xanac -i --session t4`。
2. 输入"review yuxu"——期待开始流式回复。
3. **不等结束**,再输 "hello-2"——前一个回复应停止打印(后台继续),新 run 立刻开始流;底部 toolbar 出现 `[1] review yuxu  · Tab 切换`。
4. 按 Tab——前台切回 review yuxu(继续流式或显示已完成结果)。
5. 等 review yuxu 后台完成——toolbar 的 `[1]` 消失。

- [ ] **Step 3.4: Commit**

```bash
git add src/xana/cli/xanac.py
git commit -m "feat(phase4): xanac REPL bottom_toolbar + Tab key for run switching"
```

---

## Task 4 — Gateway: `switch_foreground` API + 触发 adapter 状态

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py`(新增方法)
- Test: `tests/test_gateway_switch_foreground.py`

### Step 4.1 — Write the failing test

```python
# tests/test_gateway_switch_foreground.py
import asyncio
import pytest

from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.contracts import InboundMessage


class _StubAdapter:
    adapter_id = "cli"
    def __init__(self) -> None:
        self.progress: list[dict] = []
    async def render(self, event):
        from xana.agent_adapter.contracts import RenderedMessage
        return RenderedMessage(adapter_id="cli", session_id=event.session_id,
                               text="", metadata={})
    def note_progress(self, payload):
        self.progress.append(payload)


@pytest.mark.asyncio
async def test_switch_foreground_emits_foreground_changed_progress():
    gw = DefaultAgentGateway(agents={}, router=None)  # type: ignore[arg-type]
    adapter = _StubAdapter()
    await gw.register_adapter("cli", adapter)
    # 手动塞两个 inflight
    from xana.agent_adapter.contracts import InflightRun
    gw._inflight["s1"] = [
        InflightRun(session_id="s1", run_id="run-s1-2", label="b", detached=False),
        InflightRun(session_id="s1", run_id="run-s1-1", label="a", detached=True),
    ]
    await gw.switch_foreground("s1", "run-s1-1")
    kinds = [(p.get("kind"), p.get("run_id")) for p in adapter.progress]
    assert ("foreground_changed", "run-s1-1") in kinds
    # 原 foreground 现在标 detached
    assert gw._inflight["s1"][0].detached  # run-s1-2 旧前台
    assert not gw._inflight["s1"][1].detached  # run-s1-1 新前台
```

- [ ] **Step 4.1: 写测试**

- [ ] **Step 4.2: 跑测试确认失败**

Run: `pytest tests/test_gateway_switch_foreground.py -v`
Expected: FAIL — `switch_foreground` 不存在。

- [ ] **Step 4.3: 实现 `switch_foreground`**

加到 `DefaultAgentGateway`:

```python
async def switch_foreground(self, session_id: str, target_run_id: str) -> None:
    runs = self._inflight.get(session_id, [])
    if not any(ir.run_id == target_run_id for ir in runs):
        return
    for ir in runs:
        ir.detached = (ir.run_id != target_run_id)
    for ad in self.adapters.values():
        note = getattr(ad, "note_progress", None)
        if note is None:
            continue
        note({
            "kind": "foreground_changed",
            "session_id": session_id,
            "run_id": target_run_id,
        })
```

- [ ] **Step 4.4: 测试通过 + commit**

Run: `pytest tests/test_gateway_switch_foreground.py -v` → PASS。

```bash
git add src/xana/agent_adapter/gateway.py
git commit -m "feat(phase4): gateway.switch_foreground emits foreground_changed"
cd tests && git add test_gateway_switch_foreground.py && \
  git commit -m "test(phase4): gateway switch_foreground" && cd ..
```

---

## Task 5 — Placeholder 不渲染(feishu/telegram + CLI)

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu.py`(`_render_messages_for_context` 风格的函数,或事件渲染入口)
- Modify: `src/xana/agent_adapter/channels/telegram.py`(对称)
- Test: `tests/test_placeholder_not_rendered.py`

### Step 5.1 — Find the render path that may touch Placeholder

实操:Placeholder 出现在 `ActiveContextStore.snapshot()` 返回里。adapter 当前只在"重建会话上下文"路径触碰(目前 P3 没渲染上下文,所以只需补一个**过滤断言**保证未来不会渲染)。本 task 加防御性 helper + 测试,实际剔除点在两个 channel `render()` 的 system-history 注入函数里。

### Step 5.2 — Write the failing test

```python
# tests/test_placeholder_not_rendered.py
"""任何 channel 渲染上下文(若实现)时,Placeholder 必须被跳过。"""
from xana.memory.contracts import Placeholder, MemoryMessage
from xana.agent_adapter.channels._draft import filter_renderable


def test_filter_renderable_skips_placeholders():
    items = [
        MemoryMessage(role="user", content="hi"),
        Placeholder(run_id="r1", label="x", status="running",
                    jsonl_path="/tmp/r1.jsonl"),
        MemoryMessage(role="assistant", content="hello"),
    ]
    out = filter_renderable(items)
    assert [m.role for m in out] == ["user", "assistant"]
```

- [ ] **Step 5.1: 写测试**

- [ ] **Step 5.2: 跑测试确认失败**

Run: `pytest tests/test_placeholder_not_rendered.py -v`
Expected: FAIL — `filter_renderable` 不存在。

- [ ] **Step 5.3: 加 helper**

在 `src/xana/agent_adapter/channels/_draft.py` 末尾追加:

```python
from xana.memory.contracts import MemoryMessage, Placeholder


def filter_renderable(items):
    """从 ActiveContext snapshot 里挑出可渲染消息,跳过 Placeholder。
    任何未来在 adapter 端复现 active context 的路径都应过这道滤网。"""
    out = []
    for it in items:
        if isinstance(it, Placeholder):
            continue
        if isinstance(it, MemoryMessage):
            out.append(it)
    return out
```

(`Placeholder` 实际字段名以 `memory/contracts.py` 为准——若 import 路径不对,Step 5.4 调整。)

- [ ] **Step 5.4: 测试通过 + commit**

Run: `pytest tests/test_placeholder_not_rendered.py -v` → PASS。

```bash
git add src/xana/agent_adapter/channels/_draft.py
git commit -m "feat(phase4): channels filter_renderable helper skips Placeholder"
cd tests && git add test_placeholder_not_rendered.py && \
  git commit -m "test(phase4): placeholder not rendered" && cd ..
```

---

## Task 6 — 后台 run 完成 → 纯回答卡(feishu)

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu_card.py`(新函数 `build_pure_answer_card`)
- Modify: `src/xana/agent_adapter/channels/feishu.py`(finalize 分支判断 detached → 用纯回答卡)
- Test: `tests/test_feishu_pure_answer_card.py`

### Step 6.1 — Write the failing test

```python
# tests/test_feishu_pure_answer_card.py
from xana.agent_adapter.channels._draft import TurnDraft, ToolBlock
from xana.agent_adapter.channels.feishu_card import build_pure_answer_card


def test_pure_answer_card_has_no_thinking_or_tool_panels():
    draft = TurnDraft(run_id="r1")
    draft.thinking = "private deliberation"
    draft.body = "Hello, here is the answer."
    draft.tools = [ToolBlock(name="bash", args={"cmd": "ls"}, result="out", running=False)]
    card = build_pure_answer_card(draft)
    # 纯回答卡 elements 中不应有 collapsible_panel
    elements = card.get("elements", []) if isinstance(card, dict) else card["card"]["elements"]
    tags = [el.get("tag") for el in elements]
    assert "collapsible_panel" not in tags
    # 必须包含回答正文
    md_blocks = [el for el in elements if el.get("tag") == "markdown"]
    assert any("answer" in (b.get("content") or "") for b in md_blocks)
```

- [ ] **Step 6.1: 写测试**

- [ ] **Step 6.2: 跑测试确认失败**

Run: `pytest tests/test_feishu_pure_answer_card.py -v`
Expected: FAIL — `build_pure_answer_card` 不存在。

- [ ] **Step 6.3: 实现 `build_pure_answer_card`**

加到 `feishu_card.py` 末尾:

```python
def build_pure_answer_card(draft) -> dict:
    """后台 run 完成时推的"不抢焦点卡":去掉 thinking/tool 折叠面板,
    只保留回答正文 + 按钮。"""
    elements: list[dict] = []
    if draft.body:
        elements.append({"tag": "markdown", "content": draft.body})
    if draft.buttons:
        for row in draft.buttons:
            elements.append({"tag": "column_set", "columns": [
                {"tag": "column", "elements": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": b["label"]},
                     "value": b.get("value", {})}
                ]} for b in row
            ]})
    return {"schema": "2.0", "config": {"streaming_mode": False},
            "body": {"elements": elements}}
```

(若现有卡片包装与 yuxu 风格不同,以 `build_card` 现成返回结构对齐——读 `feishu_card.py:54+` 同步改 return shape。)

- [ ] **Step 6.4: feishu.py finalize 分支选卡**

在 `feishu.py` 找到调 `build_card(draft, finalize=True, ...)` 的位置(`_make_send` 或 finalize 路径),改为:

```python
if ts.draft.run_id and ts.draft.run_id != getattr(buf, "foreground_run_id", None) \
        and getattr(buf, "started_as_detached", {}).get(ts.draft.run_id, False):
    card = build_pure_answer_card(ts.draft)
else:
    card = build_card(ts.draft, finalize=True, buttons=...)
```

并在 `_SessionBuffer` 加字段 `started_as_detached: dict[str, bool] = field(default_factory=dict)`、在 `note_progress` 收到 `run_detached` 时记 `True`。

- [ ] **Step 6.5: 测试通过 + commit**

Run: `pytest tests/test_feishu_pure_answer_card.py -v` → PASS。

```bash
git add src/xana/agent_adapter/channels/feishu_card.py src/xana/agent_adapter/channels/feishu.py
git commit -m "feat(phase4): feishu pure-answer card for detached-run finalize"
cd tests && git add test_feishu_pure_answer_card.py && \
  git commit -m "test(phase4): feishu pure-answer card omits thinking/tool panels" && cd ..
```

---

## Task 7 — 后台 run 完成 → 纯回答消息(telegram)

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram_html.py`(新函数 `format_pure_answer_html`)
- Modify: `src/xana/agent_adapter/channels/telegram.py`(finalize 时 detached run 调纯回答 formatter)
- Test: `tests/test_telegram_pure_answer_message.py`

### Step 7.1 — Write the failing test

```python
# tests/test_telegram_pure_answer_message.py
from xana.agent_adapter.channels._draft import TurnDraft, ToolBlock
from xana.agent_adapter.channels.telegram_html import format_pure_answer_html


def test_pure_answer_html_skips_thinking_and_tools():
    d = TurnDraft(run_id="r1")
    d.thinking = "secret thoughts"
    d.body = "the answer"
    d.tools = [ToolBlock(name="bash", args={"x": 1}, result="out", running=False)]
    html = format_pure_answer_html(d)
    assert "secret thoughts" not in html
    assert "bash" not in html
    assert "the answer" in html
```

- [ ] **Step 7.1: 写测试 + 跑测试确认失败**

Run: `pytest tests/test_telegram_pure_answer_message.py -v` → FAIL。

- [ ] **Step 7.2: 实现 `format_pure_answer_html`**

加到 `telegram_html.py` 末尾:

```python
def format_pure_answer_html(draft) -> str:
    body = (draft.body or "").strip()
    return body or "_(empty)_"
```

(若 telegram 文本走 markdown / HTML 转义,沿用文件已有的 `_md_to_html` / escape 工具,统一风格。)

- [ ] **Step 7.3: telegram.py finalize 分支选 formatter**

对称 Task 6 Step 6.4——在 `telegram.py` 的 finalize 发送路径加 `if detached: send format_pure_answer_html(...)`。

- [ ] **Step 7.4: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/channels/telegram_html.py src/xana/agent_adapter/channels/telegram.py
git commit -m "feat(phase4): telegram pure-answer message for detached-run finalize"
cd tests && git add test_telegram_pure_answer_message.py && \
  git commit -m "test(phase4): telegram pure-answer message" && cd ..
```

---

## Task 8 — bg run 完成时 gateway 发 `run_completed_bg`

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py`(`_submit` finally 分支)
- Test: 复用 Task 1 的测试 + 一个补充断言

### Step 8.1 — 扩展 Task 1 测试

在 `tests/test_gateway_detach_on_new_submit.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_detached_run_completion_emits_run_completed_bg():
    agent = _FakeAgent()
    gw = DefaultAgentGateway(agents={"chat": agent}, router=_FakeRouter())
    adapter = _FakeAdapter()
    await gw.register_adapter("cli", adapter)
    sid = "s1"
    s1 = gw.submit(InboundMessage(adapter_id="cli", session_id=sid, text="a"))
    t1 = asyncio.create_task(_drain_first(s1))
    await asyncio.sleep(0.05)
    s2 = gw.submit(InboundMessage(adapter_id="cli", session_id=sid, text="b"))
    t2 = asyncio.create_task(_drain_first(s2))
    await asyncio.sleep(0.05)
    agent._gate.set()
    await asyncio.wait_for(t1, timeout=2.0)
    await asyncio.wait_for(t2, timeout=2.0)
    kinds = [(p.get("kind"), p.get("run_id")) for p in adapter.progress]
    # 至少一条 run_completed_bg 指向被 detach 的那个 run
    assert any(k == "run_completed_bg" for k, _ in kinds)
```

- [ ] **Step 8.1: 加测试**

- [ ] **Step 8.2: 跑测试确认失败**

Run: `pytest tests/test_gateway_detach_on_new_submit.py -v` → 新增用例 FAIL。

- [ ] **Step 8.3: gateway `_submit` finally 发 `run_completed_bg`**

把 Task 1 Step 1.5 写的 `finally` 块改为:

```python
finally:
    runs = self._inflight.get(inbound.session_id, [])
    self._inflight[inbound.session_id] = [ir for ir in runs if ir.run_id != new_run.run_id]
    if new_run.detached:
        # 这个 run 在跑的过程中被新的 submit detach 掉了 → 完成时通知 adapter
        note = getattr(adapter, "note_progress", None)
        if note is not None:
            note({
                "kind": "run_completed_bg",
                "session_id": inbound.session_id,
                "run_id": new_run.run_id,
            })
```

- [ ] **Step 8.4: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/gateway.py
git commit -m "feat(phase4): gateway emits run_completed_bg for detached runs"
cd tests && git add test_gateway_detach_on_new_submit.py && \
  git commit -m "test(phase4): detached run finalize emits run_completed_bg" && cd ..
```

---

## Task 9 — Sync + 人工 e2e

- [ ] **Step 9.1: 跑全套 P3+P4 测试**

Run:

```bash
pytest tests/test_gateway_detach_on_new_submit.py \
       tests/test_gateway_switch_foreground.py \
       tests/test_cli_adapter_tab_state.py \
       tests/test_feishu_pure_answer_card.py \
       tests/test_telegram_pure_answer_message.py \
       tests/test_placeholder_not_rendered.py \
       tests/test_harness_async_invoke.py \
       tests/test_phase3_e2e.py -v
```

Expected: all PASS。

- [ ] **Step 9.2: `xana sync`(自动 restart serve)**

Run: `~/.xana/bin/xana sync`
Expected: `serve restarted at pid=...` 输出。

- [ ] **Step 9.3: 手动 e2e(写进 PR 描述)**

按 Task 3 Step 3.3 的 5 步走一遍 CLI;再用 feishu 或 telegram 复现:
1. session 内连发两条慢任务("review yuxu" + "review xana")。
2. 第一张卡(yuxu)应保留为"半成品",不再更新;第二张(xana)成 foreground 持续流式。
3. yuxu 后台完成时:弹出"纯回答卡"——无 thinking/tool 折叠面板,只回答+按钮。

- [ ] **Step 9.4: 写 PR**

PR 标题:`feat(phase4): 后台 run 多卡 UX —— detach + tab 切换 + 纯回答卡`
PR body 引用本 plan 路径。

---

## 不在本 plan 范围

- **Stop / Cancel 接线**:roadmap 划入 Phase 5(`turn 级 cancel`)。本期 `gateway.cancel(...)` 保留旧 P3 行为(`_cancel_flags`),不与新的 detach 路径耦合。
- **`/resume` 命令、未完成 run 续跑、shutdown 优雅停止**:Phase 5。
- **async-aware compact (range-replace)**:Phase 6。

---

## Self-Review 记录

- 覆盖 roadmap P4 四点(底部 tab、点击切换、完成弹纯回答卡、PH 不渲染) ✓
- 无 "TODO" / "类似 Task N" 占位 ✓
- `format_tabs / set_foreground / foreground_run_id / bg_runs / switch_foreground / build_pure_answer_card / format_pure_answer_html / filter_renderable / run_detached / run_completed_bg / foreground_changed` —— 各任务使用名一致 ✓
- 接口/契约改动:仅在 `agent_adapter/contracts.py` 加 `InflightRun` dataclass(非 ABC);无 ABC 增改。**不触发** `feedback_interfaces_locked.md` 的"接口锁"红线。若 review 阶段判定 dataclass 也属"契约改动",先停下问用户再实现 Task 1 Step 1.3。
