# Phase 2: Token 预算 + 自动/手动 Compact 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `todo_ai/compact.md` 实现 token 预算与自动/手动 compact:超过阈值自动压缩上下文为 LLM 摘要,`/compact` 手动触发,脚标预算从 967k 占位换成真实 `context_window`。

**Architecture:** 共享纯函数放新文件 `harness_sdk/compact.py`(无新 class/ABC),prompt 常量放 `harness_sdk/compact_prompt.py`。触发检查内联在 `DefaultHarness._invoke()`;`/compact` 由 `DefaultCommandRouter` 复用同一组函数。压缩结果通过**已有的** `ActiveContextStore.set()` 写回(boundary marker + summary 消息 + 保留的最近 user 消息都作为普通 `MemoryMessage` 存进 `messages`,不动锁定接口,`summary` 字段弃用)。

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio, FakeModelClient/ScriptedTurn 做 LLM 测试替身。

---

## 拍板的设计决策(执行者不要再开分叉)

1. **工作区有 feishu-bot 留下的未提交半成品 compact 改动**(`config/contracts.py`、`config/loader.py`、`config/system_model.py`、`harness_sdk/default.py` 已修改 + 未跟踪的 `harness_sdk/compact_prompt.py`)。语义不符 spec(`force_compact` 做成了 bool、token 用 chars//4、无 boundary marker/circuit breaker)。**Task 0 全部回滚,全新实现。**
2. `ModelConfig`(`config/contracts.py`,锁定文件的**授权例外**,见 todo_ai/compact.md)新增 `context_window: int | None` 和 `force_compact: int | None`。`force_compact` 是 **token 阈值线**(int),不是 bool。除这两个字段外不得改任何 contracts/interfaces。
3. 阈值推导在 loader:`ctx_win = entry.context_window or 128_000`(DeepSeek 默认);`force_compact = entry.compact_override or (ctx_win - 8_000 - 13_000)`(8_000 = 默认 max_output 常量,ModelEntry 没有 max_tokens 字段);blocking 线 `ctx_win - 3_000` 在使用点推导,不进 config。warn 线不实现(脚标百分比已可见)。
4. compact 结果写回用 `ActiveContextStore.set()`:`messages = (boundary_msg, summary_msg, *recent_user_msgs)`。boundary 是 `MemoryMessage(role="system", content="对话已压缩", metadata={"compact_boundary": True})`。
5. 触发用的 token 数:`DefaultHarness` 新增 `self._session_prompt_tokens: dict[tuple[str,str], int]`,在 usage 事件处更新为 `usage.exact_input_tokens`(provider 真值);冷启动(无 usage)用 chars//4 估算。
6. Circuit breaker:每次 `_invoke` 最多自动 compact 一次(局部 flag);压缩对象是 boundary 之后的消息(`messages_since_boundary`),compact 后立即重取 snapshot。摘要生成失败(3 次重试无 `<summary>`)→ 自动 compact 放弃本回合继续正常请求(不 block 用户)。
7. blocking:若 token ≥ `ctx_win - 3_000` 且本回合已尝试过 compact 仍超 → yield error 事件提示「上下文已满,请 /clear 或换模型」并 return。
8. `/compact`:`DefaultCommandRouter` 构造注入 `model_registry` + `provider` + `model`(boot 里传,与现有 loose `Any` 风格一致),真正跑 LLM 摘要;`args` 不再当 summary 用,忽略。无历史 → 提示无可压缩内容。
9. 压缩进行中反馈:harness 通过已有 `progress_sink` 发 `ProgressEvent(message="compact_progress", payload={"kind":"compact_progress","session_id":...,"model":...,"phase":"start"|"done"|"failed"})`;`footer_from_progress` 把它渲染成 `("context","压缩中…")`,渠道无需新管道。
10. 脚标预算:`usage_progress` payload 新增 `"context_window"`;`_draft.py` 的 `CONTEXT_BUDGET_TOKENS` 从 967_000 改为 128_000 并降级为**fallback**;渠道 `_TurnState` 记住最近一次 progress payload,finalize 时优先用它算脚标。
11. post-compact 消息里 CC 的 `transcriptPath` 占位填 `"(转录不可用)"`(xana 历史镜像在内存,无文件路径)。
12. 全程不动 `memory/interfaces.py`、`harness_sdk/interfaces.py`、`agent_adapter/interfaces.py` 等锁定文件。
13. 每个 task 结束 commit;全部完成后跑全量测试(已知 1 个预先存在的失败 `test_xanac_empty_input_exits_nonzero` 可忽略),最后 `~/.xana/bin/xana sync` 部署。

---

### Task 0: 回滚 bot 半成品改动

**Files:**
- Revert: `src/xana/config/contracts.py`, `src/xana/config/loader.py`, `src/xana/config/system_model.py`, `src/xana/harness_sdk/default.py`
- Delete: `src/xana/harness_sdk/compact_prompt.py`(未跟踪)

- [ ] **Step 1: 确认这些就是唯一的未提交改动**

Run: `git status --short`
Expected: 上述 4 个 ` M` 文件 + `??` 的 compact_prompt.py / docs / todo_ai(docs 与 todo_ai 不动)。

- [ ] **Step 2: 回滚**

```bash
git checkout -- src/xana/config/contracts.py src/xana/config/loader.py src/xana/config/system_model.py src/xana/harness_sdk/default.py
rm src/xana/harness_sdk/compact_prompt.py
```

- [ ] **Step 3: 验证基线测试仍绿**

Run: `python -m pytest tests/ -x -q -k "harness or config"`
Expected: PASS(注意:回滚后 `default.py` 不再 import compact_prompt,若 import 报错说明回滚不完整)。

无 commit(工作区回到 HEAD)。

---

### Task 1: config 层 — context_window / force_compact

**Files:**
- Modify: `src/xana/config/contracts.py`(授权例外,只加 2 个字段)
- Modify: `src/xana/config/system_model.py`(ModelEntry 加 `context_window`)
- Modify: `src/xana/config/loader.py`(`_resolve_default_model` 推导阈值)
- Test: `tests/test_config_compact_thresholds.py`

- [ ] **Step 1: 写失败测试**

```python
"""context_window/force_compact 从 system.json 推导进 ModelConfig。"""
from xana.config.loader import _resolve_default_model
from xana.config.system_model import SystemConfig


def _system(**entry_extra) -> SystemConfig:
    return SystemConfig(**{
        "models": [{"name": "m1", "type": "anthropic_deepseek", "api_key": "k", **entry_extra}],
        "default_model": {"chat": "m1", "agent": "m1"},
    })


def test_default_thresholds_for_deepseek() -> None:
    mc = _resolve_default_model(_system())
    assert mc.context_window == 128_000
    assert mc.force_compact == 128_000 - 8_000 - 13_000  # 107_000


def test_entry_context_window_overrides_default() -> None:
    mc = _resolve_default_model(_system(context_window=64_000))
    assert mc.context_window == 64_000
    assert mc.force_compact == 64_000 - 8_000 - 13_000


def test_compact_override_wins() -> None:
    mc = _resolve_default_model(_system(compact_override=50_000))
    assert mc.force_compact == 50_000


def test_fallback_model_has_no_thresholds() -> None:
    sys_cfg = SystemConfig(models=[], default_model={"chat": None, "agent": None})
    mc = _resolve_default_model(sys_cfg)
    assert mc.context_window is None and mc.force_compact is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config_compact_thresholds.py -q`
Expected: FAIL(ModelConfig 无 context_window 字段 / TypeError)。

- [ ] **Step 3: 实现**

`config/contracts.py` 的 `ModelConfig` 加(只加这两行,其余不动):

```python
    context_window: int | None = None
    force_compact: int | None = None
```

`config/system_model.py` 的 `ModelEntry` 在 `compact_override` 下加:

```python
    context_window: Optional[int] = None
```

`config/loader.py` 顶部常量区加:

```python
_DEFAULT_CONTEXT_WINDOW = 128_000  # DeepSeek 缺省;entry.context_window 可覆盖
_DEFAULT_MAX_OUTPUT = 8_000
_COMPACT_HEADROOM = 13_000  # CC 风格: force_compact = ctx - max_output - 13K
```

`_resolve_default_model` 改为:

```python
def _resolve_default_model(system: SystemConfig) -> ModelConfig:
    chat_name = system.default_model.chat
    if chat_name is not None:
        entry = system.model(chat_name)
        if entry is not None:
            ctx_win = entry.context_window or _DEFAULT_CONTEXT_WINDOW
            force_at = entry.compact_override or (ctx_win - _DEFAULT_MAX_OUTPUT - _COMPACT_HEADROOM)
            return ModelConfig(
                provider=entry.type, model=entry.name,
                context_window=ctx_win, force_compact=force_at,
            )
    return ModelConfig(provider=_FALLBACK_PROVIDER, model=_FALLBACK_MODEL)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config_compact_thresholds.py tests/test_arch_layers.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/xana/config tests/test_config_compact_thresholds.py
git commit -m "feat(config): context_window + force_compact thresholds on ModelConfig (authorized compact extension)"
```

---

### Task 2: compact prompt 常量

**Files:**
- Create: `src/xana/harness_sdk/compact_prompt.py`
- Test: `tests/test_compact_prompt.py`

- [ ] **Step 1: 写失败测试**

```python
from xana.harness_sdk.compact_prompt import (
    COMPACT_BOUNDARY_MARKER, POST_COMPACT_TEMPLATE, get_compact_prompt,
)


def test_boundary_marker() -> None:
    assert COMPACT_BOUNDARY_MARKER == "对话已压缩"


def test_prompt_assembles_preamble_body_trailer() -> None:
    p = get_compact_prompt()
    assert p.startswith("重要:仅回复纯文本。")
    assert "<analysis>" in p and "<summary>" in p
    assert p.rstrip().endswith("工具调用将被拒绝,你将无法完成任务。")


def test_post_compact_template_slots() -> None:
    msg = POST_COMPACT_TEMPLATE.format(summary="S", transcript="T")
    assert "S" in msg and "T" in msg and msg.startswith("此会话正在从上一次因上下文不足而中断的对话中继续")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compact_prompt.py -q`
Expected: FAIL (ModuleNotFoundError)。

- [ ] **Step 3: 实现**

新建 `src/xana/harness_sdk/compact_prompt.py`,纯字符串常量,**逐字**抄 `todo_ai/compact.md` 的「Prompt 中文翻译」一节(该文件 35–140 行有全文,不要改写、不要增删段落):

```python
"""Compact prompt 常量 — 抄 CC services/compact/prompt.ts 的中文翻译。

全文来源: todo_ai/compact.md。纯常量,不定义 class。
"""
from __future__ import annotations

COMPACT_BOUNDARY_MARKER = "对话已压缩"

NO_TOOLS_PREAMBLE = """重要:仅回复纯文本。禁止调用任何工具。

- 禁止使用 Read、Bash、Grep、Glob、Edit、Write 或任何其他工具。
- 上述对话中已包含你所需的全部上下文。
- 工具调用将被拒绝且浪费你唯一的回合——你将无法完成任务。
- 你的全部回复必须为纯文本:一个 <analysis> 块后跟一个 <summary> 块。"""

BASE_COMPACT_PROMPT = """\
<todo_ai/compact.md 第 47–122 行的 BASE_COMPACT_PROMPT 全文,逐字复制,
从「你的任务是创建一份迄今为止对话的详细摘要」到
「请根据迄今为止的对话提供你的摘要,遵循此结构并确保你的回复精确且详尽。」>"""

NO_TOOLS_TRAILER = """提醒:禁止调用任何工具。仅回复纯文本——一个 <analysis> 块后跟一个 <summary> 块。工具调用将被拒绝,你将无法完成任务。"""

POST_COMPACT_TEMPLATE = """此会话正在从上一次因上下文不足而中断的对话中继续。以下摘要涵盖了对话的早期部分。

{summary}

如果你需要压缩前的具体细节(如确切的代码片段、错误信息或你生成的内容),请阅读完整转录记录:{transcript}

最近的消息被逐字保留。
继续对话,无需向用户提出任何进一步的问题。直接继续——不要确认摘要,不要复述正在发生的事情,不要以"我将继续"或类似语句开头。如同中断从未发生一样继续上一个任务。"""


def get_compact_prompt() -> str:
    return NO_TOOLS_PREAMBLE + "\n\n" + BASE_COMPACT_PROMPT + "\n\n" + NO_TOOLS_TRAILER
```

注意:`BASE_COMPACT_PROMPT` 的占位必须替换为 compact.md 中的**完整原文**(含 `<example>` 块),实现时打开 `todo_ai/compact.md` 复制。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_compact_prompt.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/compact_prompt.py tests/test_compact_prompt.py
git commit -m "feat(harness): compact prompt constants (CC prompt.ts Chinese port)"
```

---

### Task 3: compact 纯函数(harness_sdk/compact.py)

**Files:**
- Create: `src/xana/harness_sdk/compact.py`
- Test: `tests/test_compact_functions.py`

- [ ] **Step 1: 写失败测试**

```python
import asyncio

from xana.harness_sdk.compact import (
    build_post_compact_context, estimate_tokens, generate_summary,
    messages_since_boundary, recent_user_messages,
)
from xana.harness_sdk.compact_prompt import COMPACT_BOUNDARY_MARKER
from xana.llm_driver.fake_client import FakeModelClient, ScriptedTurn
from xana.memory.contracts import MemoryMessage


def _m(role: str, content: str, **meta) -> MemoryMessage:
    return MemoryMessage(role=role, content=content, metadata=meta)


def test_estimate_tokens_chars_div_4() -> None:
    assert estimate_tokens((_m("user", "x" * 400),)) == 100


def test_messages_since_boundary_returns_tail() -> None:
    msgs = (_m("user", "old"), _m("system", COMPACT_BOUNDARY_MARKER, compact_boundary=True),
            _m("user", "new1"), _m("assistant", "new2"))
    assert [m.content for m in messages_since_boundary(msgs)] == ["new1", "new2"]


def test_messages_since_boundary_no_boundary_returns_all() -> None:
    msgs = (_m("user", "a"), _m("assistant", "b"))
    assert messages_since_boundary(msgs) == msgs


def test_recent_user_messages_respects_budget_and_order() -> None:
    msgs = (_m("user", "u1"), _m("assistant", "a"), _m("user", "x" * 8), _m("user", "u3"))
    kept = recent_user_messages(msgs, budget_tokens=3)  # u3=1t, x*8=2t, u1 超出
    assert [m.content for m in kept] == ["x" * 8, "u3"]


def test_generate_summary_extracts_summary_block() -> None:
    client = FakeModelClient(ScriptedTurn(
        text_deltas=("<analysis>think</analysis>\n", "<summary>压缩摘要</summary>"),
    ))
    out = asyncio.run(generate_summary(
        client, provider="fake", model="m", messages=(_m("user", "hi"),)))
    assert out == "压缩摘要"


def test_generate_summary_retries_then_none() -> None:
    client = FakeModelClient(
        ScriptedTurn(text_deltas=("no tags",)),
        ScriptedTurn(text_deltas=("still none",)),
        ScriptedTurn(text_deltas=("nope",)),
    )
    out = asyncio.run(generate_summary(
        client, provider="fake", model="m", messages=(_m("user", "hi"),)))
    assert out is None


def test_build_post_compact_context_shape() -> None:
    ctx = build_post_compact_context(
        session_id="s", agent_name="chat", summary="SUM",
        recent=(_m("user", "recent"),))
    assert ctx.messages[0].metadata.get("compact_boundary") is True
    assert ctx.messages[0].content == COMPACT_BOUNDARY_MARKER
    assert "SUM" in ctx.messages[1].content and ctx.messages[1].role == "user"
    assert ctx.messages[2].content == "recent"
    assert ctx.summary == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compact_functions.py -q`
Expected: FAIL (ModuleNotFoundError)。

- [ ] **Step 3: 实现**

新建 `src/xana/harness_sdk/compact.py`:

```python
"""Compact 共享纯函数 — auto-compact(DefaultHarness 内联触发)与 /compact 共用。

无 class、无 ABC;状态写回走已有 ActiveContextStore.set()。
boundary/summary/保留消息全部是普通 MemoryMessage,summary 字段不再使用。
"""
from __future__ import annotations

import re
from typing import Sequence

from xana.harness_sdk.compact_prompt import (
    COMPACT_BOUNDARY_MARKER,
    POST_COMPACT_TEMPLATE,
    get_compact_prompt,
)
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.memory.contracts import ActiveContext, MemoryMessage

_CHARS_PER_TOKEN = 4  # 与 DefaultUsageAccumulator 同源启发式
RECENT_KEEP_TOKENS = 20_000  # Codex 风格: 最近 user 消息逐字保留预算
BLOCKING_HEADROOM = 3_000    # blocking 线 = context_window - 3K
_COMPACT_RETRIES = 3
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_TRANSCRIPT_PLACEHOLDER = "(转录不可用)"


def estimate_tokens(messages: Sequence[MemoryMessage]) -> int:
    return max(1, sum(len(m.content or "") for m in messages) // _CHARS_PER_TOKEN)


def messages_since_boundary(messages: tuple[MemoryMessage, ...]) -> tuple[MemoryMessage, ...]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].metadata.get("compact_boundary"):
            return messages[i + 1:]
    return messages


def recent_user_messages(
    messages: Sequence[MemoryMessage], budget_tokens: int = RECENT_KEEP_TOKENS
) -> tuple[MemoryMessage, ...]:
    kept: list[MemoryMessage] = []
    used = 0
    for m in reversed(messages):
        if m.role != "user":
            continue
        cost = max(1, len(m.content or "") // _CHARS_PER_TOKEN)
        if used + cost > budget_tokens:
            break
        kept.append(m)
        used += cost
    return tuple(reversed(kept))


async def generate_summary(
    client, *, provider: str, model: str, messages: Sequence[MemoryMessage]
) -> str | None:
    """同模型生成摘要: 无 tools、单回合;<summary> 缺失重试,3 次后放弃返回 None。"""
    model_msgs = tuple(
        ModelMessage(
            role=m.role if m.role in ("user", "assistant", "system", "tool") else "system",
            content=m.content,
        )
        for m in messages
    ) + (ModelMessage(role="user", content=get_compact_prompt()),)
    req = ModelRequest(provider=provider, model=model, messages=model_msgs, tools_schema=())
    for _ in range(_COMPACT_RETRIES):
        parts: list[str] = []
        async for event in client.stream(req):
            if event.kind == "text_delta":
                parts.append(event.payload.get("text", ""))
            elif event.kind in ("done", "error"):
                break
        match = _SUMMARY_RE.search("".join(parts))
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def build_post_compact_context(
    *, session_id: str, agent_name: str, summary: str, recent: tuple[MemoryMessage, ...]
) -> ActiveContext:
    boundary = MemoryMessage(
        role="system", content=COMPACT_BOUNDARY_MARKER, metadata={"compact_boundary": True}
    )
    summary_msg = MemoryMessage(
        role="user",
        content=POST_COMPACT_TEMPLATE.format(summary=summary, transcript=_TRANSCRIPT_PLACEHOLDER),
    )
    return ActiveContext(
        session_id=session_id, agent_name=agent_name,
        messages=(boundary, summary_msg) + recent,
    )
```

注意:`MemoryMessage`/`ActiveContext` 的构造参数以 `src/xana/memory/contracts.py` 现有定义为准(只读,不修改);若 `ActiveContext` 有必填字段超出上述,补默认值。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_compact_functions.py tests/test_arch_layers.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/compact.py tests/test_compact_functions.py
git commit -m "feat(harness): compact pure functions — boundary scan, retention, LLM summary with retry"
```

---

### Task 4: DefaultHarness 自动 compact + token 跟踪 + context_window 进 usage_progress

**Files:**
- Modify: `src/xana/harness_sdk/default.py`
- Modify: `src/xana/agents/chat_agent.py`(config_snapshot 加阈值)
- Modify: `src/xana/runtime/boot.py`(把 ModelConfig 阈值传给 ChatAgent)
- Test: `tests/test_harness_auto_compact.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest

from xana.harness_sdk.compact_prompt import COMPACT_BOUNDARY_MARKER
from xana.harness_sdk.contracts import HarnessContext, HarnessRequest, ProgressEvent
from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.interfaces import ProgressSink
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import InMemoryActiveContextStore, InMemoryHistoryMirror
from xana.memory.contracts import MemoryMessage


class _Sink(ProgressSink):
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def report(self, event: ProgressEvent) -> None:
        self.events.append(event)


def _ctx(store, reg, sink=None, *, ctx_win=None, force_at=None) -> HarnessContext:
    snap = {"provider": "fake"}
    if ctx_win is not None:
        snap["context_window"] = ctx_win
        snap["force_compact"] = force_at
    return HarnessContext(
        session_id="s1", agent_name="chat", cwd="/", model="fake-m",
        config_snapshot=snap,
        active_context_store=store, history_mirror=InMemoryHistoryMirror(),
        model_registry=reg, progress_sink=sink,
    )


async def _seed_history(store, n_chars: int) -> None:
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="x" * n_chars))


@pytest.mark.asyncio
async def test_auto_compact_triggers_over_threshold() -> None:
    store = InMemoryActiveContextStore()
    await _seed_history(store, 4_000)  # ≈1000 tokens > force_at=500
    client = FakeModelClient(
        ScriptedTurn(text_deltas=("<summary>历史摘要</summary>",)),  # compact 调用
        ScriptedTurn(text_deltas=("正常回答",)),                      # 正式 turn
    )
    reg = InMemoryModelRegistry(); await reg.register("fake", client)
    sink = _Sink()
    h = DefaultHarness()
    events = []
    async for ev in h.invoke(HarnessRequest(session_id="s1", agent_name="chat", user_input="hi"),
                             _ctx(store, reg, sink, ctx_win=100_000, force_at=500)):
        events.append(ev)
    assert events[-1].kind == "final"
    snap = await store.get("s1", "chat")
    contents = [m.content for m in snap.messages]
    assert COMPACT_BOUNDARY_MARKER in contents          # boundary 写回
    assert any("历史摘要" in c for c in contents)         # summary 消息写回
    phases = [e.payload.get("phase") for e in sink.events
              if e.payload.get("kind") == "compact_progress"]
    assert phases[:1] == ["start"] and "done" in phases


@pytest.mark.asyncio
async def test_no_compact_under_threshold() -> None:
    store = InMemoryActiveContextStore()
    await _seed_history(store, 40)
    client = FakeModelClient(ScriptedTurn(text_deltas=("ok",)))
    reg = InMemoryModelRegistry(); await reg.register("fake", client)
    h = DefaultHarness()
    async for _ in h.invoke(HarnessRequest(session_id="s1", agent_name="chat", user_input="hi"),
                            _ctx(store, reg, ctx_win=100_000, force_at=500)):
        pass
    snap = await store.get("s1", "chat")
    assert all(not m.metadata.get("compact_boundary") for m in snap.messages)


@pytest.mark.asyncio
async def test_compact_failure_does_not_block_turn() -> None:
    store = InMemoryActiveContextStore()
    await _seed_history(store, 4_000)
    # 3 次 compact 重试都无 <summary>,第 4 个 script 是正式 turn
    client = FakeModelClient(
        ScriptedTurn(text_deltas=("no tags",)), ScriptedTurn(text_deltas=("no",)),
        ScriptedTurn(text_deltas=("no",)), ScriptedTurn(text_deltas=("正常回答",)),
    )
    reg = InMemoryModelRegistry(); await reg.register("fake", client)
    h = DefaultHarness()
    kinds = []
    async for ev in h.invoke(HarnessRequest(session_id="s1", agent_name="chat", user_input="hi"),
                             _ctx(store, reg, ctx_win=100_000, force_at=500)):
        kinds.append(ev.kind)
    assert kinds[-1] == "final"  # 失败放弃 compact,正常回答


@pytest.mark.asyncio
async def test_usage_progress_carries_context_window() -> None:
    store = InMemoryActiveContextStore()
    client = FakeModelClient(ScriptedTurn(text_deltas=("hi",), usage_input_tokens=10,
                                          usage_output_tokens=2))
    reg = InMemoryModelRegistry(); await reg.register("fake", client)
    sink = _Sink()
    h = DefaultHarness()
    async for _ in h.invoke(HarnessRequest(session_id="s1", agent_name="chat", user_input="hi"),
                            _ctx(store, reg, sink, ctx_win=128_000, force_at=107_000)):
        pass
    usage = [e for e in sink.events if e.payload.get("kind") == "usage_progress"]
    assert usage and usage[-1].payload["context_window"] == 128_000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_harness_auto_compact.py -q`
Expected: FAIL(compact 未触发 / payload 无 context_window)。

- [ ] **Step 3: 实现 harness**

`harness_sdk/default.py`:

a) imports 加:

```python
from xana.harness_sdk.compact import (
    BLOCKING_HEADROOM, build_post_compact_context, estimate_tokens,
    generate_summary, messages_since_boundary, recent_user_messages,
)
```

b) `__init__` 加:

```python
        self._session_prompt_tokens: dict[tuple[str, str], int] = {}
```

c) `_invoke` 的 for 循环里、`snapshot = await context.active_context_store.get(...)` 取完 `history_msgs` 之后(原 bot 半成品所在位置),插入(`compact_attempted = False` 在循环外、`turn_start` 旁初始化):

```python
            # ---- auto-compact: token 数 vs force_compact 线 ----
            ctx_win = context.config_snapshot.get("context_window")
            force_at = context.config_snapshot.get("force_compact")
            if ctx_win and force_at and history_msgs:
                key = (session_id, agent_name)
                tokens = self._session_prompt_tokens.get(key) or estimate_tokens(history_msgs)
                live = messages_since_boundary(history_msgs)
                if tokens >= int(force_at) and live and not compact_attempted:
                    compact_attempted = True
                    await self._report_compact(context, session_id, run_id, "start")
                    summary = await generate_summary(
                        client, provider=provider, model=context.model, messages=live)
                    if summary is None:
                        await self._report_compact(context, session_id, run_id, "failed")
                    else:
                        await context.active_context_store.set(build_post_compact_context(
                            session_id=session_id, agent_name=agent_name,
                            summary=summary, recent=recent_user_messages(live)))
                        self._session_prompt_tokens.pop(key, None)
                        await self._report_compact(context, session_id, run_id, "done")
                        snapshot = await context.active_context_store.get(session_id, agent_name)
                        history_msgs = snapshot.messages if snapshot is not None else ()
                        summary_field = snapshot.summary if snapshot is not None else ""
                elif tokens >= int(ctx_win) - BLOCKING_HEADROOM and compact_attempted:
                    await self._fire_turn_end(
                        ext, session_id=session_id, agent_name=agent_name, run_id=run_id,
                        thread_store=thread_store, turn_store=turn_store, status="error")
                    yield HarnessEvent(kind="error", session_id=session_id, run_id=run_id,
                                       payload={"error": "上下文已满且压缩未能降低占用,请 /clear 或更换模型"})
                    return
```

(注意现有变量名是 `summary` 用于 snapshot.summary——为避免遮蔽,把原 `summary = snapshot.summary` 行重命名为 `summary_field` 并同步 `_assemble_for_model(..., summary=summary_field)`。)

d) 加 helper 方法:

```python
    async def _report_compact(self, context: HarnessContext, session_id: str,
                              run_id: str, phase: str) -> None:
        if context.progress_sink is None:
            return
        await context.progress_sink.report(ProgressEvent(
            session_id=session_id, run_id=run_id, message="compact_progress",
            payload={"kind": "compact_progress", "session_id": session_id,
                     "model": context.model, "phase": phase},
        ))
```

e) usage 事件分支(`event.kind == "usage"`)里、report 之前加:

```python
                        if usage.exact_input_tokens:
                            self._session_prompt_tokens[(session_id, agent_name)] = usage.exact_input_tokens
```

payload 里加一行:

```python
                                    "context_window": context.config_snapshot.get("context_window"),
```

- [ ] **Step 4: 实现 chat_agent + boot 接线**

`agents/chat_agent.py`:`DefaultAgentRequestMapper.__init__` 与 `ChatAgent.__init__` 各加 kwargs `context_window: int | None = None, force_compact: int | None = None` 并透传;`to_harness_context` 的 `config_snapshot` 初始化改为:

```python
        config_snapshot: dict[str, Any] = {
            "provider": self.provider,
            "context_window": self.context_window,
            "force_compact": self.force_compact,
        }
```

`runtime/boot.py`:`ChatAgent(...)` 调用处加:

```python
        context_window=runtime_config.default_model.context_window,
        force_compact=runtime_config.default_model.force_compact,
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_harness_auto_compact.py tests/test_harness_progress_emission.py tests/ -q -k "harness or boot or agent"`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/xana/harness_sdk/default.py src/xana/agents/chat_agent.py src/xana/runtime/boot.py tests/test_harness_auto_compact.py
git commit -m "feat(harness): auto-compact on force_compact threshold + blocking line + context_window in usage_progress"
```

---

### Task 5: /compact 命令接 LLM 摘要

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`(`_cmd_compact` + 构造注入)
- Modify: `src/xana/runtime/boot.py`(router 注入 model_registry/provider/model)
- Test: `tests/test_command_compact.py`

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest

from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.contracts import InboundMessage
from xana.harness_sdk.compact_prompt import COMPACT_BOUNDARY_MARKER
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.contracts import MemoryMessage


def _inbound(text: str) -> InboundMessage:
    return InboundMessage(channel="test", session_id="s1", user_id="u1", text=text)


async def _router(client) -> tuple[DefaultCommandRouter, InMemoryActiveContextStore]:
    store = InMemoryActiveContextStore()
    reg = InMemoryModelRegistry(); await reg.register("fake", client)
    router = DefaultCommandRouter(
        active_context_store=store, model_registry=reg, provider="fake", model="fake-m")
    return router, store


@pytest.mark.asyncio
async def test_compact_runs_llm_and_rewrites_context() -> None:
    router, store = await _router(FakeModelClient(
        ScriptedTurn(text_deltas=("<summary>摘要内容</summary>",))))
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="历史消息"))
    res = await router.route_command(_inbound("/compact"))
    assert res.ok
    snap = await store.get("s1", "chat")
    contents = [m.content for m in snap.messages]
    assert contents[0] == COMPACT_BOUNDARY_MARKER
    assert any("摘要内容" in c for c in contents)
    assert "历史消息" in contents  # 最近 user 消息逐字保留


@pytest.mark.asyncio
async def test_compact_empty_history_reports_nothing_to_do() -> None:
    router, _ = await _router(FakeModelClient(ScriptedTurn(text_deltas=("x",))))
    res = await router.route_command(_inbound("/compact"))
    assert not res.ok and "无可压缩" in res.message


@pytest.mark.asyncio
async def test_compact_summary_failure_reports_error() -> None:
    router, store = await _router(FakeModelClient(
        ScriptedTurn(text_deltas=("no",)), ScriptedTurn(text_deltas=("no",)),
        ScriptedTurn(text_deltas=("no",))))
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="hi"))
    res = await router.route_command(_inbound("/compact"))
    assert not res.ok and "失败" in res.message
```

(`InboundMessage` 构造参数以 `agent_adapter/contracts.py` 现有定义为准,与 `tests/test_command_router*.py` 既有写法保持一致。)

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_command_compact.py -q`
Expected: FAIL(router 不收 model_registry / _cmd_compact 行为旧)。

- [ ] **Step 3: 实现**

`agent_adapter/commands.py` 构造器加参数(loose `Any`,与现有风格一致):

```python
        model_registry: Any = None,
        provider: str = "",
        model: str = "",
```

并保存到 `self.model_registry / self.provider / self.model`。`_cmd_compact` 替换为:

```python
    async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
        from xana.harness_sdk.compact import (
            build_post_compact_context, generate_summary,
            messages_since_boundary, recent_user_messages,
        )
        if self.active_context_store is None:
            return CommandResult(ok=False, message="active_context_store 未配置")
        if self.model_registry is None:
            return CommandResult(ok=False, message="model_registry 未配置,无法压缩")
        snapshot = await self.active_context_store.get(inbound.session_id, "chat")
        live = messages_since_boundary(snapshot.messages) if snapshot else ()
        if not live:
            return CommandResult(ok=False, message="无可压缩内容")
        client = await self.model_registry.get(self.provider)
        if client is None:
            return CommandResult(ok=False, message=f"provider {self.provider!r} 未注册")
        summary = await generate_summary(
            client, provider=self.provider, model=self.model, messages=live)
        if summary is None:
            return CommandResult(ok=False, message="压缩失败:模型未返回 <summary>,请重试")
        recent = recent_user_messages(live)
        await self.active_context_store.set(build_post_compact_context(
            session_id=inbound.session_id, agent_name="chat",
            summary=summary, recent=recent))
        return CommandResult(
            ok=True, message=f"[compacted] 历史已压缩,逐字保留最近 {len(recent)} 条消息")
```

`runtime/boot.py` 的 `DefaultCommandRouter(...)` 调用加:

```python
        model_registry=model_registry,
        provider=provider,
        model=model,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_command_compact.py tests/ -q -k "command or boot or arch"`
Expected: PASS(若既有 router 测试断言了旧 `/compact` 行为,按新行为更新该断言)。

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/commands.py src/xana/runtime/boot.py tests/test_command_compact.py
git commit -m "feat(commands): /compact runs real LLM summarization via shared compact functions"
```

---

### Task 6: 脚标预算真实化 + 压缩中提示

**Files:**
- Modify: `src/xana/agent_adapter/channels/_draft.py`
- Modify: `src/xana/agent_adapter/channels/feishu.py`、`src/xana/agent_adapter/channels/telegram.py`(_TurnState 记住最近 progress payload;finalize 优先用它)
- Test: `tests/test_draft_footer_budget.py`

- [ ] **Step 1: 写失败测试**

```python
from xana.agent_adapter.channels._draft import (
    CONTEXT_BUDGET_TOKENS, footer_from_progress,
)


def test_footer_uses_payload_context_window() -> None:
    pairs = dict(footer_from_progress({
        "kind": "usage_progress", "model": "m", "tokens_in": 64_000,
        "context_window": 128_000,
    }))
    assert pairs["context"] == "64.0k/128k (50%)"


def test_footer_falls_back_to_default_budget() -> None:
    pairs = dict(footer_from_progress({
        "kind": "usage_progress", "model": "m", "tokens_in": 12_800}))
    assert f"/{CONTEXT_BUDGET_TOKENS // 1000}k" in pairs["context"]
    assert CONTEXT_BUDGET_TOKENS == 128_000  # 967k 占位退役


def test_footer_shows_compacting_phase() -> None:
    pairs = dict(footer_from_progress({
        "kind": "compact_progress", "model": "m", "phase": "start"}))
    assert pairs["context"] == "压缩中…"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_draft_footer_budget.py -q`
Expected: FAIL(预算还是 967k / 无 compact_progress 分支)。

- [ ] **Step 3: 实现 _draft.py**

```python
CONTEXT_BUDGET_TOKENS = 128_000  # fallback;真实预算来自 usage_progress payload 的 context_window
```

`_context_pair` 改为接受预算:

```python
def _context_pair(tin: int, budget: int | None = None) -> tuple[str, str]:
    b = budget or CONTEXT_BUDGET_TOKENS
    pct = round(tin * 100 / b)
    return ("context", f"{_fmt_k(tin)}/{b // 1000}k ({pct}%)")
```

`footer_from_progress` 改为:

```python
def footer_from_progress(payload: dict, *, model: str | None = None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    m = model or payload.get("model")
    if m:
        pairs.append(("model", str(m)))
    if payload.get("kind") == "compact_progress":
        if payload.get("phase") in ("start", None):
            pairs.append(("context", "压缩中…"))
        return pairs
    tin = int(payload.get("tokens_in") or 0)
    if tin:
        pairs.append(_context_pair(tin, payload.get("context_window")))
    return pairs
```

`build_footer_meta(usage)` 加可选参数 `context_window: int | None = None` 并把 `_context_pair(tin)` 改为 `_context_pair(tin, context_window)`。

- [ ] **Step 4: 渠道 finalize 优先用最近 progress payload**

`feishu.py` 与 `telegram.py`:`_TurnState` 加字段 `last_progress: dict | None = None`;`note_progress` 路由到 turn 时存 `turn.last_progress = dict(payload)`(已有 finalized 守卫保持);finalize 处把 `build_footer_meta(usage)` 改为:

```python
footer = (footer_from_progress(turn.last_progress)
          if turn.last_progress and turn.last_progress.get("kind") == "usage_progress"
          else build_footer_meta(usage))
```

(两个文件的具体变量名以现状为准——该 finalize 段是 Phase 1 刚写的,模式一致。)

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_draft_footer_budget.py tests/ -q -k "draft or feishu or telegram or footer"`
Expected: PASS(若既有脚标测试硬编码 967k,按 128k fallback 更新)。

- [ ] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/channels tests/test_draft_footer_budget.py
git commit -m "feat(channels): footer budget from real context_window + compacting indicator"
```

---

### Task 7: 全量回归 + 部署

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -q`
Expected: 除已知的 `test_xanac_empty_input_exits_nonzero`(预先存在,与本计划无关)外全绿。

- [ ] **Step 2: 部署**

```bash
~/.xana/bin/xana sync
```

Expected: `serve restarted (pid ...)`;`tail -5 ~/.xana/logs/serve.log` 无异常。

- [ ] **Step 3: 报告**

向用户报告:auto-compact 阈值(默认 128k 窗口 → 107k 触发线)、`/compact` 用法、脚标变化;请用户在 feishu/tg 实测(长对话观察脚标百分比与「压缩中…」)。
