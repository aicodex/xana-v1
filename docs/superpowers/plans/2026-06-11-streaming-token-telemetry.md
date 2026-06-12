# Streaming Token Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface running token counts during streaming so the CLI footer can refresh live (in the REPL Plan 1 lands) and so any future channel adapter has the same telemetry. Token source of truth is the provider's `ModelEvent(kind="usage")` (cc-style — no local tokenization).

**Architecture:**
- Implement `DefaultUsageAccumulator` (the existing `UsageAccumulator` ABC in `harness_sdk/interfaces.py` has no concrete impl). It owns:
  1. `estimate_before_request()` — sums character lengths of the assembled prompt and divides by ~4 for a rough pre-flight estimate. Returns `UsageSnapshot(estimated_input_tokens=...)`.
  2. `apply_model_event(current, event)` — when `event.kind=="usage"`, merge `event.usage.input_tokens` / `output_tokens` onto current, preserving previously-known fields (cc's `updateUsage` rule).
- Wire the accumulator into `DefaultHarness._invoke`: replace the inline `replace(usage, ...)` with a call to `accumulator.apply_model_event`, then emit a `ProgressEvent` on each update.
- Add `progress_sink` plumbing in `boot()` and a new `AdapterProgressSink` that bridges progress events into the adapter — adapters that care (CliAdapter interactive mode) consume them; oneshot CliAdapter ignores them. **No changes to `HarnessContext` or `ProgressEvent` contracts** — payload dict carries `{tokens_in, tokens_out, elapsed_s}`.
- CliAdapter gains a `note_progress(payload)` hook that, in interactive mode, redraws the footer line below the prompt using `prompt_toolkit` `Application.invalidate()`. Oneshot mode is unchanged; finalize still prints the static card.

**Tech Stack:** Python 3.10+, existing xana interfaces, optional prompt_toolkit redraw API.

**File structure:**
- Create: `src/xana/harness_sdk/usage.py` — `DefaultUsageAccumulator`
- Create: `src/xana/agent_adapter/progress.py` — `AdapterProgressSink`
- Modify: `src/xana/harness_sdk/default.py` — use accumulator + emit ProgressEvent
- Modify: `src/xana/runtime/boot.py` — instantiate sink + accumulator, attach to HarnessContext
- Modify: `src/xana/agent_adapter/cli.py` — `note_progress` + interactive redraw
- Modify: `src/xana/agents/chat_agent.py` — pass progress_sink through HarnessContext
- Test: `tests/test_default_usage_accumulator.py`
- Test: `tests/test_harness_progress_emission.py`
- Test: `tests/test_cli_adapter_progress_footer.py`

---

### Task 1: DefaultUsageAccumulator

**Files:**
- Create: `src/xana/harness_sdk/usage.py`
- Test: `tests/test_default_usage_accumulator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_default_usage_accumulator.py`:

```python
from __future__ import annotations

import pytest

from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.harness_sdk.usage import DefaultUsageAccumulator
from xana.llm_driver.contracts import ModelEvent, ModelMessage, ModelUsage
from xana.state.contracts import UsageSnapshot


def _ctx() -> HarnessContext:
    return HarnessContext(session_id="s1", agent_name="chat", cwd="/", model="m")


@pytest.mark.asyncio
async def test_estimate_before_request_uses_char_quarter_heuristic() -> None:
    acc = DefaultUsageAccumulator()
    msgs = (
        ModelMessage(role="system", content="x" * 40),  # 10 tokens
        ModelMessage(role="user", content="y" * 80),    # 20 tokens
    )
    snap = await acc.estimate_before_request(
        request=HarnessRequest(session_id="s1", agent_name="chat", user_input=""),
        context=_ctx(),
        messages=msgs,
    )
    assert snap.model == "m"
    # 120 chars / 4 = 30
    assert snap.estimated_input_tokens == 30


@pytest.mark.asyncio
async def test_apply_usage_event_overrides_with_exact() -> None:
    acc = DefaultUsageAccumulator()
    cur = UsageSnapshot(model="m", estimated_input_tokens=42)
    ev = ModelEvent(kind="usage", usage=ModelUsage(input_tokens=234, output_tokens=567))
    nxt = await acc.apply_model_event(cur, ev)
    assert nxt.exact_input_tokens == 234
    assert nxt.exact_output_tokens == 567
    assert nxt.estimated_input_tokens == 42  # preserved


@pytest.mark.asyncio
async def test_apply_non_usage_event_is_noop() -> None:
    acc = DefaultUsageAccumulator()
    cur = UsageSnapshot(model="m", exact_input_tokens=100)
    for kind in ("text_delta", "tool_call", "done", "error", "rate_limited"):
        nxt = await acc.apply_model_event(cur, ModelEvent(kind=kind))
        assert nxt == cur


@pytest.mark.asyncio
async def test_apply_usage_event_with_only_output_keeps_previous_input() -> None:
    """cc rule: input_tokens is set once at message_start, then output_tokens climbs."""
    acc = DefaultUsageAccumulator()
    cur = UsageSnapshot(model="m", exact_input_tokens=234, exact_output_tokens=10)
    ev = ModelEvent(kind="usage", usage=ModelUsage(input_tokens=None, output_tokens=42))
    nxt = await acc.apply_model_event(cur, ev)
    assert nxt.exact_input_tokens == 234
    assert nxt.exact_output_tokens == 42
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_default_usage_accumulator.py -v`
Expected: fail — module missing.

- [ ] **Step 3: Implement DefaultUsageAccumulator**

Create `src/xana/harness_sdk/usage.py`:

```python
"""DefaultUsageAccumulator: provider-event-driven token tracking.

Mirrors cc's updateUsage rule (services/api/claude.ts):
  - Take exact_input_tokens/exact_output_tokens from ModelUsage when present
    (None or 0 means "carry over the previous value").
  - estimated_input_tokens is set once via estimate_before_request() and
    carried forward; never overwritten by exact-value updates.

We trust the provider's usage events as source of truth; we do not run a
local tokenizer. If a provider reports nothing, the snapshot stays at the
pre-flight estimate.
"""
from __future__ import annotations

from dataclasses import replace

from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.harness_sdk.interfaces import UsageAccumulator
from xana.llm_driver.contracts import ModelEvent, ModelMessage
from xana.state.contracts import UsageSnapshot


_CHARS_PER_TOKEN = 4  # rough English heuristic, cc uses same for its prompt-size status line


class DefaultUsageAccumulator(UsageAccumulator):
    async def estimate_before_request(
        self,
        request: HarnessRequest,
        context: HarnessContext,
        messages: tuple[ModelMessage, ...],
    ) -> UsageSnapshot:
        total_chars = sum(len(m.content or "") for m in messages)
        est = max(1, total_chars // _CHARS_PER_TOKEN)
        return UsageSnapshot(
            model=context.model,
            provider=context.config_snapshot.get("provider"),
            estimated_input_tokens=est,
        )

    async def apply_model_event(self, current: UsageSnapshot, event: ModelEvent) -> UsageSnapshot:
        if event.kind != "usage" or event.usage is None:
            return current
        u = event.usage
        new_in = u.input_tokens if (u.input_tokens is not None and u.input_tokens > 0) else current.exact_input_tokens
        new_out = u.output_tokens if u.output_tokens is not None else current.exact_output_tokens
        return replace(current, exact_input_tokens=new_in, exact_output_tokens=new_out)
```

- [ ] **Step 4: Check UsageSnapshot has estimated_input_tokens field**

Read `src/xana/state/contracts.py`. If `estimated_input_tokens` is NOT already a field on `UsageSnapshot`, **STOP** — that is an interface change and requires user permission. Report the gap and wait.

If the field exists, proceed. If not, narrow the implementation to `exact_*` fields and drop the estimate path (the harness will skip estimation and start with zeros).

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_default_usage_accumulator.py -v`
Expected: all pass (modulo any field gap discovered in step 4)

- [ ] **Step 6: Commit**

```bash
git add src/xana/harness_sdk/usage.py tests/test_default_usage_accumulator.py
git commit -m "feat(harness): DefaultUsageAccumulator (cc updateUsage parity)"
```

---

### Task 2: Wire DefaultUsageAccumulator into DefaultHarness

**Files:**
- Modify: `src/xana/harness_sdk/default.py`
- Test: `tests/test_default_harness_uses_accumulator.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_default_harness_uses_accumulator.py`:

```python
from __future__ import annotations

import pytest

from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import InMemoryActiveContextStore, InMemoryHistoryMirror


@pytest.mark.asyncio
async def test_final_usage_matches_provider_reported(monkeypatch) -> None:
    """Provider says 234/567 → final HarnessEvent.usage carries 234/567."""
    client = FakeModelClient(ScriptedTurn(
        text_deltas=("hello",),
        usage_input_tokens=234,
        usage_output_tokens=567,
    ))
    reg = InMemoryModelRegistry()
    await reg.register("fake", client)

    h = DefaultHarness()
    ctx = HarnessContext(
        session_id="s1", agent_name="chat", cwd="/", model="fake-m",
        config_snapshot={"provider": "fake"},
        active_context_store=InMemoryActiveContextStore(),
        history_mirror=InMemoryHistoryMirror(),
        model_registry=reg,
    )
    req = HarnessRequest(session_id="s1", agent_name="chat", user_input="hi")
    final = None
    async for event in h.invoke(req, ctx):
        if event.kind == "final":
            final = event
    assert final is not None and final.usage is not None
    assert final.usage.exact_input_tokens == 234
    assert final.usage.exact_output_tokens == 567
```

- [ ] **Step 2: Run to verify pass or fail**

Run: `pytest tests/test_default_harness_uses_accumulator.py -v`

If it already passes (because DefaultHarness already replaces usage inline), skip to step 4. If not, proceed.

- [ ] **Step 3: Replace inline usage update with accumulator call**

In `src/xana/harness_sdk/default.py`, add import:

```python
from xana.harness_sdk.usage import DefaultUsageAccumulator
```

Add to `DefaultHarness.__init__`:

```python
def __init__(self) -> None:
    self.last_assembled_messages: tuple[ModelMessage, ...] | None = None
    self.usage_accumulator = DefaultUsageAccumulator()
```

In `_invoke`, where the existing code does `elif event.kind == "usage" and event.usage is not None:`, replace the entire branch with:

```python
elif event.kind == "usage":
    usage = await self.usage_accumulator.apply_model_event(usage, event)
```

Keep the existing branches for `text_delta`, `tool_call`, `done`, `error`.

- [ ] **Step 4: Run all tests**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/test_default_harness_uses_accumulator.py
git commit -m "refactor(harness): route usage updates through DefaultUsageAccumulator"
```

---

### Task 3: Emit ProgressEvent on every usage update

**Files:**
- Modify: `src/xana/harness_sdk/default.py`
- Test: `tests/test_harness_progress_emission.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_harness_progress_emission.py`:

```python
from __future__ import annotations

import pytest

from xana.harness_sdk.contracts import HarnessContext, HarnessRequest, ProgressEvent
from xana.harness_sdk.default import DefaultHarness
from xana.harness_sdk.interfaces import ProgressSink
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import InMemoryActiveContextStore, InMemoryHistoryMirror


class _CapturingSink(ProgressSink):
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def report(self, event: ProgressEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_harness_reports_progress_on_usage_event() -> None:
    sink = _CapturingSink()
    client = FakeModelClient(ScriptedTurn(
        text_deltas=("hi",),
        usage_input_tokens=10, usage_output_tokens=20,
    ))
    reg = InMemoryModelRegistry()
    await reg.register("fake", client)
    h = DefaultHarness()
    ctx = HarnessContext(
        session_id="s1", agent_name="chat", cwd="/", model="fake-m",
        config_snapshot={"provider": "fake"},
        active_context_store=InMemoryActiveContextStore(),
        history_mirror=InMemoryHistoryMirror(),
        model_registry=reg,
        progress_sink=sink,
    )
    async for _ in h.invoke(HarnessRequest(session_id="s1", agent_name="chat", user_input="hi"), ctx):
        pass
    usage_events = [e for e in sink.events if e.payload.get("kind") == "usage_progress"]
    assert len(usage_events) >= 1
    last = usage_events[-1]
    assert last.payload["tokens_in"] == 10
    assert last.payload["tokens_out"] == 20
    assert isinstance(last.payload["elapsed_s"], float)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_harness_progress_emission.py -v`
Expected: fail — no progress events emitted.

- [ ] **Step 3: Emit ProgressEvent in _invoke**

In `src/xana/harness_sdk/default.py`, near the top of `_invoke`, capture turn start time:

```python
import time
turn_start = time.monotonic()
```

In the `elif event.kind == "usage":` branch after the accumulator call, add:

```python
if context.progress_sink is not None:
    await context.progress_sink.report(ProgressEvent(
        session_id=session_id, run_id=run_id,
        message="usage_progress",
        payload={
            "kind": "usage_progress",
            "tokens_in": usage.exact_input_tokens or 0,
            "tokens_out": usage.exact_output_tokens or 0,
            "elapsed_s": time.monotonic() - turn_start,
        },
    ))
```

Import `ProgressEvent`:

```python
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest, ProgressEvent
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_harness_progress_emission.py -v && pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/test_harness_progress_emission.py
git commit -m "feat(harness): emit usage_progress ProgressEvent on each provider usage update"
```

---

### Task 4: AdapterProgressSink bridge

**Files:**
- Create: `src/xana/agent_adapter/progress.py`
- Test: `tests/test_adapter_progress_sink.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_adapter_progress_sink.py`:

```python
from __future__ import annotations

import pytest

from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.progress import AdapterProgressSink
from xana.harness_sdk.contracts import ProgressEvent


@pytest.mark.asyncio
async def test_sink_forwards_usage_progress_to_adapter_note() -> None:
    import io
    adapter = CliAdapter(out=io.StringIO(), adapter_id="cli", session_id="s1")
    sink = AdapterProgressSink({"cli": adapter})
    await sink.report(ProgressEvent(
        session_id="s1", run_id="r1",
        message="usage_progress",
        payload={"kind": "usage_progress", "tokens_in": 12, "tokens_out": 34, "elapsed_s": 1.5},
    ))
    assert adapter.last_progress is not None
    assert adapter.last_progress["tokens_in"] == 12
    assert adapter.last_progress["tokens_out"] == 34


@pytest.mark.asyncio
async def test_sink_ignores_non_usage_progress() -> None:
    import io
    adapter = CliAdapter(out=io.StringIO(), adapter_id="cli", session_id="s1")
    sink = AdapterProgressSink({"cli": adapter})
    await sink.report(ProgressEvent(
        session_id="s1", run_id="r1",
        message="something_else", payload={"kind": "other"},
    ))
    assert adapter.last_progress is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_adapter_progress_sink.py -v`
Expected: fail — module + `last_progress`/`note_progress` missing.

- [ ] **Step 3: Add note_progress to CliAdapter**

In `src/xana/agent_adapter/cli.py`, add to `__init__`:

```python
self.last_progress: dict[str, Any] | None = None
self._progress_callback = None  # set by REPL to trigger live redraw
```

Add method:

```python
def note_progress(self, payload: dict[str, Any]) -> None:
    """Adapter hook for progress updates. Stores latest; REPL may register a
    callback to repaint the footer in place."""
    self.last_progress = dict(payload)
    if self._progress_callback is not None:
        self._progress_callback(payload)

def set_progress_callback(self, cb) -> None:
    self._progress_callback = cb
```

- [ ] **Step 4: Implement AdapterProgressSink**

Create `src/xana/agent_adapter/progress.py`:

```python
"""AdapterProgressSink: route harness ProgressEvents into adapters.

Only `usage_progress` payloads are forwarded. Other kinds (e.g. internal
checkpoint hooks) are dropped silently. Adapter must expose `note_progress`
to opt in; adapters without it are skipped.
"""
from __future__ import annotations

from typing import Mapping

from xana.harness_sdk.contracts import ProgressEvent
from xana.harness_sdk.interfaces import ProgressSink


class AdapterProgressSink(ProgressSink):
    def __init__(self, adapters: Mapping[str, object]) -> None:
        self.adapters = adapters

    async def report(self, event: ProgressEvent) -> None:
        if event.payload.get("kind") != "usage_progress":
            return
        for adapter in self.adapters.values():
            note = getattr(adapter, "note_progress", None)
            if note is not None:
                note(event.payload)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_adapter_progress_sink.py -v && pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/cli.py src/xana/agent_adapter/progress.py tests/test_adapter_progress_sink.py
git commit -m "feat(adapter): AdapterProgressSink bridges usage_progress to adapter.note_progress"
```

---

### Task 5: Wire ProgressSink into boot + ChatAgent

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Modify: `src/xana/agents/chat_agent.py`
- Test: `tests/test_boot_progress_pipeline.py`

- [ ] **Step 1: Write failing end-to-end test**

Create `tests/test_boot_progress_pipeline.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot
from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.contracts import InboundMessage
import io


@pytest.mark.asyncio
async def test_end_to_end_progress_reaches_adapter(tmp_path: Path) -> None:
    result = await boot(project_root=tmp_path)
    adapter = CliAdapter(out=io.StringIO(), adapter_id="cli", session_id="s1")
    await adapter.start()
    await result.gateway.register_adapter("cli", adapter)
    # Boot defaults to FakeModelClient when no api key — which emits a usage event.
    inbound = InboundMessage(adapter_id="cli", session_id="s1", text="hello")
    async for _ in result.gateway.submit(inbound):
        pass
    assert adapter.last_progress is not None
    assert "tokens_in" in adapter.last_progress
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_boot_progress_pipeline.py -v`
Expected: fail — `adapter.last_progress is None` (boot doesn't wire the sink yet).

- [ ] **Step 3: Wire AdapterProgressSink in boot**

In `src/xana/runtime/boot.py`, add import:

```python
from xana.agent_adapter.progress import AdapterProgressSink
```

After `gateway = DefaultAgentGateway(...)` and before the `BootResult(...)` return, add:

```python
progress_sink = AdapterProgressSink(gateway.adapters)
```

Pass `progress_sink=progress_sink` into `ChatAgent(...)` (you'll add the parameter in step 4).

Add `progress_sink: ProgressSink` to `BootResult` and include it in the returned instance. Import `ProgressSink`:

```python
from xana.harness_sdk.interfaces import ProgressSink
```

- [ ] **Step 4: ChatAgent forwards progress_sink into HarnessContext**

In `src/xana/agents/chat_agent.py`, add `progress_sink` parameter to `ChatAgent.__init__` (default `None`). When building the HarnessContext, include:

```python
progress_sink=self.progress_sink,
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_boot_progress_pipeline.py -v && pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/xana/runtime/boot.py src/xana/agents/chat_agent.py tests/test_boot_progress_pipeline.py
git commit -m "feat(boot): wire AdapterProgressSink so usage_progress reaches CliAdapter"
```

---

### Task 6: CliAdapter footer reflects last_progress

**Files:**
- Modify: `src/xana/agent_adapter/cli.py`
- Test: `tests/test_cli_adapter_progress_footer.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_cli_adapter_progress_footer.py`:

```python
from __future__ import annotations

import io

import pytest

from xana.agents.contracts import AgentEvent
from xana.agent_adapter.cli import CliAdapter


@pytest.mark.asyncio
async def test_final_footer_uses_last_progress_when_no_harness_event() -> None:
    """If progress arrived during streaming, the finalize card should display it
    even when the test-constructed AgentEvent(final) has no harness_event."""
    buf = io.StringIO()
    adapter = CliAdapter(out=buf, adapter_id="cli", session_id="s1")
    await adapter.render(AgentEvent(kind="draft", session_id="s1", payload={"text": "answer"}))
    adapter.note_progress({"kind": "usage_progress", "tokens_in": 42, "tokens_out": 13, "elapsed_s": 0.5})
    await adapter.render(AgentEvent(kind="final", session_id="s1", payload={"text": "answer"}))
    out = buf.getvalue()
    assert "tokens: 42 in / 13 out" in out
    assert "elapsed: 0.5s" in out
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_cli_adapter_progress_footer.py -v`
Expected: fail — finalize footer suppressed because no harness_event and progress is ignored.

- [ ] **Step 3: Modify _collect_footer_meta to fall back to last_progress**

In `src/xana/agent_adapter/cli.py`, replace `_collect_footer_meta`:

```python
def _collect_footer_meta(self, event: AgentEvent) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    usage = getattr(event.harness_event, "usage", None) if event.harness_event else None
    if usage is not None:
        if usage.model:
            pairs.append(("model", str(usage.model)))
        tin = usage.exact_input_tokens
        tout = usage.exact_output_tokens
        if tin is not None or tout is not None:
            pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
    elif self.last_progress is not None:
        tin = self.last_progress.get("tokens_in")
        tout = self.last_progress.get("tokens_out")
        if tin is not None or tout is not None:
            pairs.append(("tokens", f"{tin or 0} in / {tout or 0} out"))
    if pairs and self.last_progress is not None:
        elapsed = float(self.last_progress.get("elapsed_s", 0.0))
        pairs.append(("elapsed", f"{elapsed:.1f}s"))
    elif self._turn_start_ts is not None and pairs:
        import time as _t
        elapsed = max(0.0, _t.monotonic() - self._turn_start_ts)
        pairs.append(("elapsed", f"{elapsed:.1f}s"))
    return pairs
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cli_adapter_progress_footer.py -v && pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/cli.py tests/test_cli_adapter_progress_footer.py
git commit -m "feat(cli): finalize footer falls back to last_progress when harness_event absent"
```

---

### Task 7: Live footer redraw in REPL mode (depends on Plan 1)

**Files:**
- Modify: `src/xana/cli/xanac.py`

- [ ] **Step 1: Decide based on Plan 1 status**

If Plan 1 (REPL) is NOT merged yet, skip to step 5 — there is no place to redraw a live footer in oneshot mode. Tests in tasks 1-6 already cover the data flow.

If Plan 1 IS merged, continue.

- [ ] **Step 2: Register progress callback in REPL loop**

In `src/xana/cli/xanac.py` `_repl_loop`, after constructing the `PromptSession` and before the input loop, register a callback on the adapter (if accessible — the REPL would need a handle; if not, retrieve from the boot result):

```python
from prompt_toolkit.application import get_app_or_none

def _redraw_footer(payload: dict) -> None:
    app = get_app_or_none()
    if app is not None:
        app.invalidate()
    # The prompt_toolkit bottom_toolbar callable will be re-invoked on
    # invalidate(); it reads from adapter.last_progress.
```

- [ ] **Step 3: Add bottom_toolbar to PromptSession**

When creating `PromptSession`, pass:

```python
def _bottom_toolbar():
    p = adapter.last_progress
    if not p:
        return "ready"
    return f"tokens: {p.get('tokens_in',0)} in / {p.get('tokens_out',0)} out · elapsed: {p.get('elapsed_s',0):.1f}s"

prompt = PromptSession("> ", bottom_toolbar=_bottom_toolbar)
```

- [ ] **Step 4: Smoke test in REPL**

Run `xanac --session live-test`, send a message that takes long enough to stream. Watch the bottom toolbar update as tokens climb.

- [ ] **Step 5: Commit (or skip-commit message)**

If you implemented the redraw:

```bash
git add src/xana/cli/xanac.py
git commit -m "feat(xanac): REPL bottom_toolbar shows running tokens during streaming"
```

If Plan 1 isn't merged yet, no commit — note in the followup that this task is deferred until REPL exists.

---

### Task 8: Regression sweep

- [ ] **Step 1: Full pytest**

Run: `pytest -q`
Expected: 0 failures.

- [ ] **Step 2: Smoke test oneshot footer**

Run: `xanac "what time is it"` (with real DS provider configured).
Expected: streaming text appears, then `―` divider + `_model · tokens · elapsed_` footer with non-zero tokens.

- [ ] **Step 3: Inspect git log**

Run: `git log --oneline -10`
Expected: 5-7 plan commits visible.

---

**End of plan.** Total expected commits: ~6.
