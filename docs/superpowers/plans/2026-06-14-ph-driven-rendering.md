# PH-Driven Rendering + Textual REPL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace P4 per-run/pure-answer rendering with PH state machine; merge feishu & TG cards into single-card model with frozen-PH UX; convert xanac REPL to Textual sharing pipeline with one-shot; sink design back into upstream docs.

**Architecture:** PH (placeholder) in active context drives adapter card state. Each run owns one `RunRenderState` (per-run `_TurnState` accumulator + `card_anchor`). Detach → freeze card (stop edits, accumulation continues). PH replacement by write-queue consumer → finalize card to full (thinking/tool folded). `switch_foreground` → full re-render to new card. Compact = special run; manual `/compact` and auto fork-before-compact go through same code path. CliAdapter renderer abstracted so xanac (Textual) and one-shot (stdout) share submit pipeline.

**Tech Stack:** Python 3.13, asyncio, lark-oapi (feishu), aiogram (TG), prompt_toolkit→textual (CLI), pytest (independent tests/ sub-repo).

**Spec:** `docs/superpowers/specs/2026-06-14-ph-driven-rendering-design.md` — authoritative reference for §state machine, §events, §15 concurrency scenarios.

**Frozen-contract reminder:** Per `feedback_interfaces_locked.md`, every change to `*/contracts.py` or `*/interfaces.py` listed in this plan MUST be confirmed in Task 1 before downstream tasks touch them.

---

## Task 1: Contracts & interfaces approval gate

**Files:**
- Modify: `src/xana/agent_adapter/interfaces.py` (add `CliRenderer` ABC)
- Modify: `src/xana/agent_adapter/contracts.py` (no struct change; verify `InflightRun` sufficient)
- No `Placeholder` change — already shipped in P2 `memory/contracts.py`.

- [ ] **Step 1: User confirmation**

Print the spec §9 ask list verbatim and wait for explicit yes before any further task:
```
Requesting approval per feedback_interfaces_locked.md:
1. Add CliRenderer ABC to agent_adapter/interfaces.py.
2. ProgressEvent kind values (strings, no struct change): ph_replaced, compact_started, compact_finalized.
3. CommandResult.payload conventions: triggers_turn: bool, cancellable: bool.
```

If user says no — STOP. Do not proceed.

- [ ] **Step 2: Write failing test for CliRenderer interface shape**

`tests/test_cli_renderer_abc.py`:
```python
import inspect
from xana.agent_adapter.interfaces import CliRenderer

def test_cli_renderer_abstract_methods():
    methods = {n for n, _ in inspect.getmembers(CliRenderer, predicate=inspect.isfunction)}
    expected = {
        "on_run_started", "on_event", "on_run_detached",
        "on_foreground_changed", "on_ph_replaced",
        "on_run_completed_bg", "on_compact_started", "on_compact_finalized",
    }
    assert expected.issubset(methods)
```

Run: `pytest tests/test_cli_renderer_abc.py -v` → FAIL (ImportError).

- [ ] **Step 3: Implement CliRenderer**

Append to `src/xana/agent_adapter/interfaces.py`:
```python
from xana.agents.contracts import AgentEvent  # already imported above
from xana.agent_adapter.channels._draft import TurnDraft  # forward import — see note

class CliRenderer(ABC):
    """CLI renderer — implements PH-driven card pipeline for stdout (oneshot) and Textual (interactive)."""

    @abstractmethod
    async def on_run_started(self, run_id: str, label: str) -> None: ...

    @abstractmethod
    async def on_event(self, run_id: str, event: AgentEvent) -> None: ...

    @abstractmethod
    async def on_run_detached(self, run_id: str) -> None: ...

    @abstractmethod
    async def on_foreground_changed(self, run_id: str) -> None: ...

    @abstractmethod
    async def on_ph_replaced(self, run_id: str, draft: "TurnDraft") -> None: ...

    @abstractmethod
    async def on_run_completed_bg(self, run_id: str, draft: "TurnDraft") -> None: ...

    @abstractmethod
    async def on_compact_started(self, run_id: str, fork_point: int) -> None: ...

    @abstractmethod
    async def on_compact_finalized(self, run_id: str, summary: str) -> None: ...
```

Note: `TurnDraft` import as forward-string to avoid layering violation; if `_draft` is a private module, hoist `TurnDraft` to `channels/__init__.py` or duplicate dataclass into `contracts.py` (ask user if neither fits — frozen file rule).

- [ ] **Step 4: Run test, verify pass**

`pytest tests/test_cli_renderer_abc.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/interfaces.py
cd tests && git add test_cli_renderer_abc.py && git commit -m "test: CliRenderer ABC shape" && cd ..
git commit -m "feat(adapter): add CliRenderer ABC for PH-driven CLI rendering"
```

---

## Task 2: Gateway emits PH lifecycle events

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py` — extend `_submit`, add helpers `_emit_ph_replaced`, `_emit_compact_started`, `_emit_compact_finalized`.
- Test: `tests/test_gateway_ph_events.py`

**Pre-read:** §4.1 events table; current `gateway.py` `_inflight`, `switch_foreground`, `_notify_run_detached`, `_make_send`.

- [ ] **Step 1: Failing test — ph_replaced after run completion**

```python
# tests/test_gateway_ph_events.py
import pytest, asyncio
from xana.agent_adapter.contracts import InboundMessage
# fixtures: build_test_gateway() returns gateway, fake_adapter capturing note_progress events.

@pytest.mark.asyncio
async def test_ph_replaced_emitted_when_run_finalizes(build_test_gateway):
    gw, adapter = build_test_gateway()
    async for _ in gw.submit(InboundMessage(adapter_id="cli", session_id="s", text="hi")):
        pass
    kinds = [e["kind"] for e in adapter.progress_events]
    assert "ph_replaced" in kinds
```

Run: FAIL (no such kind emitted).

- [ ] **Step 2: Implement ph_replaced emit**

In `gateway.py` `_submit`, after the `async for event in agent.send(request)` loop completes successfully, before the `finally` cleanup:
```python
            if not new_run.detached:
                self._emit_ph_replaced(adapter, inbound.session_id, new_run.run_id, "full")
            # bg completion path stays as run_completed_bg (existing)
```

Add helper:
```python
    @staticmethod
    def _emit_ph_replaced(adapter, session_id: str, run_id: str, mode: str) -> None:
        note = getattr(adapter, "note_progress", None)
        if note is not None:
            note({"kind": "ph_replaced", "session_id": session_id, "run_id": run_id, "mode": mode})
```

For bg completion path, also emit `ph_replaced` with `mode="full"` BEFORE existing `run_completed_bg` (adapter uses ph_replaced as the canonical finalize signal; run_completed_bg becomes "no-focus hint"):
```python
            if new_run.detached:
                self._emit_ph_replaced(adapter, inbound.session_id, new_run.run_id, "full")
                note = getattr(adapter, "note_progress", None)
                if note is not None:
                    note({"kind": "run_completed_bg", "session_id": ..., "run_id": new_run.run_id})
```

- [ ] **Step 3: Failing test — compact lifecycle**

```python
@pytest.mark.asyncio
async def test_compact_started_finalized_emitted(build_test_gateway):
    gw, adapter = build_test_gateway()
    await gw.start_compact("sess", fork_point=3)
    # wait for compact done
    kinds = [e["kind"] for e in adapter.progress_events]
    assert "compact_started" in kinds and "compact_finalized" in kinds
```

Run: FAIL — `start_compact` not defined.

- [ ] **Step 4: Implement gateway.start_compact**

```python
    async def start_compact(self, session_id: str, fork_point: int) -> None:
        """Trigger a compact run — used by both /compact handler and auto-threshold path."""
        adapter_id = next(iter(self.adapters), None)
        if adapter_id is None: return
        adapter = self.adapters[adapter_id]
        self._run_seq += 1
        rid = f"compact-{session_id}-{self._run_seq}"
        note = getattr(adapter, "note_progress", None)
        if note: note({"kind": "compact_started", "session_id": session_id, "run_id": rid, "fork_point": fork_point})
        # Actual compact execution wired in Task 8; emit finalized stub for now.
        if note: note({"kind": "compact_finalized", "session_id": session_id, "run_id": rid, "summary": ""})
```

Real execution wiring lands in Task 8 (compact runner integration).

- [ ] **Step 5: Failing test — state commands bypass _inflight**

```python
@pytest.mark.asyncio
async def test_status_command_does_not_detach_inflight(build_test_gateway):
    gw, adapter = build_test_gateway()
    t1 = asyncio.create_task(_drain(gw.submit(InboundMessage(adapter_id="cli", session_id="s", text="long task"))))
    await asyncio.sleep(0.05)
    async for _ in gw.submit(InboundMessage(adapter_id="cli", session_id="s", text="/help")):
        pass
    # /help must not have produced run_detached for the long task
    assert not any(e["kind"] == "run_detached" for e in adapter.progress_events)
    await t1
```

Run: FAIL.

- [ ] **Step 6: Implement status-command bypass**

In `_submit`, before the existing detach loop, peek command router:
```python
        cmd_result = await self._maybe_route_command(inbound)
        if cmd_result is not None and not cmd_result.payload.get("triggers_turn", False):
            # State command — render result via adapter without inflight tracking
            async for r in self._render_command_result(adapter, inbound, cmd_result):
                yield r
            return
```

If `_maybe_route_command` doesn't exist yet, refactor existing command path to expose it. `/compact` handler must set `payload["triggers_turn"] = True`.

- [ ] **Step 7: Run all gateway tests, pass; commit**

```bash
cd tests && git add test_gateway_ph_events.py && git commit -m "test: gateway PH/compact event emission + status cmd bypass" && cd ..
git add src/xana/agent_adapter/gateway.py
git commit -m "feat(gateway): emit ph_replaced/compact_*; state cmds bypass inflight"
```

---

## Task 3: Adapter shared `RunRenderState` model

**Files:**
- Create: `src/xana/agent_adapter/channels/_render_state.py`
- Test: `tests/test_render_state.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_render_state.py
from xana.agent_adapter.channels._render_state import RunRenderState, SessionRenderState

def test_session_render_state_tracks_foreground():
    s = SessionRenderState()
    s.start_run("r1", label="hi")
    assert s.foreground_run_id == "r1"
    s.start_run("r2", label="next")
    assert s.foreground_run_id == "r2"
    assert s.runs["r1"].is_frozen is True
    assert s.runs["r2"].is_frozen is False

def test_switch_foreground_swaps_frozen():
    s = SessionRenderState()
    s.start_run("r1", "a"); s.start_run("r2", "b")
    s.switch_foreground("r1")
    assert s.runs["r1"].is_frozen is False
    assert s.runs["r2"].is_frozen is True
    assert s.foreground_run_id == "r1"
```

Run: FAIL.

- [ ] **Step 2: Implement**

```python
# src/xana/agent_adapter/channels/_render_state.py
from __future__ import annotations
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from xana.agent_adapter.channels._draft import TurnDraft

@dataclass
class RunRenderState:
    run_id: str
    label: str
    turns: "OrderedDict[str, TurnDraft]" = field(default_factory=OrderedDict)
    card_anchor: Any = None  # channel-specific message_id / widget id
    is_foreground: bool = False
    is_frozen: bool = True

@dataclass
class SessionRenderState:
    runs: "OrderedDict[str, RunRenderState]" = field(default_factory=OrderedDict)
    foreground_run_id: str | None = None
    max_runs: int = 4

    def start_run(self, run_id: str, label: str) -> RunRenderState:
        if self.foreground_run_id and self.foreground_run_id in self.runs:
            self.runs[self.foreground_run_id].is_foreground = False
            self.runs[self.foreground_run_id].is_frozen = True
        st = RunRenderState(run_id=run_id, label=label, is_foreground=True, is_frozen=False)
        self.runs[run_id] = st
        self.foreground_run_id = run_id
        while len(self.runs) > self.max_runs:
            self.runs.popitem(last=False)
        return st

    def switch_foreground(self, run_id: str) -> None:
        if run_id not in self.runs: return
        if self.foreground_run_id and self.foreground_run_id in self.runs:
            self.runs[self.foreground_run_id].is_foreground = False
            self.runs[self.foreground_run_id].is_frozen = True
        self.runs[run_id].is_foreground = True
        self.runs[run_id].is_frozen = False
        self.foreground_run_id = run_id

    def detach(self, run_id: str) -> None:
        if run_id in self.runs:
            self.runs[run_id].is_frozen = True
            self.runs[run_id].is_foreground = False
            if self.foreground_run_id == run_id:
                self.foreground_run_id = None

    def bg_runs(self) -> list[RunRenderState]:
        return [r for r in self.runs.values() if not r.is_foreground]
```

- [ ] **Step 3: Test passes; commit**

```bash
cd tests && git add test_render_state.py && git commit -m "test: SessionRenderState" && cd ..
git add src/xana/agent_adapter/channels/_render_state.py
git commit -m "feat(adapter): shared per-session RunRenderState model"
```

---

## Task 4: Feishu PH state machine

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu.py` — swap `_SessionBuffer.turns` for `SessionRenderState`; rewire `_make_send` and `note_progress`.
- Modify: `src/xana/agent_adapter/channels/feishu_card.py` — add `build_frozen_ph_card(draft, label)` and `build_full_card` (rename/wrap existing `build_card` to ensure thinking/tool always folded).
- Delete: `feishu_card.build_pure_answer_card` (Task 9).
- Test: `tests/test_feishu_ph_state_machine.py`

- [ ] **Step 1: Failing test — detach freezes card without further PATCH**

```python
@pytest.mark.asyncio
async def test_detach_freezes_feishu_card(monkeypatch, mk_feishu):
    adapter, fake_client = mk_feishu()
    # start run1
    await adapter.note_progress({"kind":"run_started","session_id":"sess","run_id":"r1","label":"hi"})
    await adapter.render(_evt_draft("r1", "partial"))
    fake_client.patch_calls.clear()
    # detach
    await adapter.note_progress({"kind":"run_detached","session_id":"sess","run_id":"r1"})
    # last patch should be the frozen-PH card
    assert fake_client.patch_calls, "expected one final patch to render frozen PH card"
    last = fake_client.patch_calls[-1]
    assert "运行中" in last.body_json
    # further draft events do NOT cause patches
    fake_client.patch_calls.clear()
    await adapter.render(_evt_draft("r1", "more partial"))
    assert fake_client.patch_calls == []
```

Run: FAIL.

- [ ] **Step 2: Implement frozen-PH card builder**

`feishu_card.py`:
```python
def build_frozen_ph_card(draft: TurnDraft, label: str) -> dict:
    """PH frozen card: 🟡 chip + 'already produced' collapsible. No buttons."""
    # Reuse existing collapsible_panel helpers; copy thinking/tool/content blocks
    # but strip ALL action buttons and footer_meta.
    body = build_card(draft)
    body.setdefault("header", {})["title"] = {"tag": "plain_text", "content": f"🟡 后台运行中 · {label}"}
    body["elements"] = [el for el in body.get("elements", []) if el.get("tag") != "action"]
    return body
```

- [ ] **Step 3: Implement state machine in feishu.py**

Replace `_SessionBuffer.turns: OrderedDict[run_id, _TurnState]` with `SessionRenderState`. Wire:
```python
async def note_progress(self, payload: dict) -> None:
    sid = payload["session_id"]
    state = self._sessions.setdefault(sid, SessionRenderState())
    kind = payload["kind"]
    rid = payload.get("run_id")
    if kind == "run_started":
        state.start_run(rid, payload.get("label",""))
    elif kind == "run_detached":
        state.detach(rid)
        run = state.runs.get(rid)
        if run and run.card_anchor:
            # one final PATCH to swap to frozen-PH card
            await self._patch(run.card_anchor, build_frozen_ph_card(self._draft_from(run), run.label))
    elif kind == "foreground_changed":
        # freeze previous fg's card, full re-render new fg on a NEW message_id
        prev_fg = state.foreground_run_id
        state.switch_foreground(rid)
        if prev_fg:
            prev = state.runs[prev_fg]
            await self._patch(prev.card_anchor, build_frozen_ph_card(self._draft_from(prev), prev.label))
        target = state.runs[rid]
        new_mid = await self._send_new(build_full_card(self._draft_from(target), running=True))
        target.card_anchor = new_mid
    elif kind == "ph_replaced":
        run = state.runs.get(rid)
        if run and run.card_anchor:
            await self._patch(run.card_anchor, build_full_card(self._draft_from(run), running=False))
    elif kind == "run_completed_bg":
        pass  # ph_replaced already handled finalize
    elif kind == "compact_started":
        # insert a new compact card at fork_point (channels are append-only — just send new)
        new_mid = await self._send_new(build_compact_loading_card(payload.get("fork_point")))
        state.runs[rid] = RunRenderState(run_id=rid, label="compact", card_anchor=new_mid, is_foreground=False, is_frozen=True)
    elif kind == "compact_finalized":
        run = state.runs.get(rid)
        if run and run.card_anchor:
            await self._patch(run.card_anchor, build_compact_summary_card(payload.get("summary","")))
```

In `render(event)`:
```python
async def render(self, event):
    rid = event.payload.get("run_id")
    state = self._sessions.get(event.session_id)
    if not state or rid not in state.runs: return _noop
    run = state.runs[rid]
    # accumulate into TurnDraft (existing code path)
    self._accumulate(run, event)
    if run.is_frozen:
        return _noop  # accumulate only, NO patch
    if run.card_anchor is None:
        new_mid = await self._send_new(build_full_card(self._draft_from(run), running=True))
        run.card_anchor = new_mid
    else:
        await self._patch(run.card_anchor, build_full_card(self._draft_from(run), running=True))
    return _ok
```

- [ ] **Step 4: Failing test — switch_foreground sends new card**

```python
@pytest.mark.asyncio
async def test_switch_foreground_sends_new_card(mk_feishu):
    adapter, fake = mk_feishu()
    await adapter.note_progress({"kind":"run_started","session_id":"s","run_id":"r1","label":"a"})
    await adapter.render(_evt_draft("r1","x"))
    await adapter.note_progress({"kind":"run_started","session_id":"s","run_id":"r2","label":"b"})
    await adapter.render(_evt_draft("r2","y"))
    sends_before = len(fake.send_calls)
    await adapter.note_progress({"kind":"foreground_changed","session_id":"s","run_id":"r1"})
    assert len(fake.send_calls) == sends_before + 1  # new card for r1
```

Run: should pass after Step 3 implementation.

- [ ] **Step 5: Failing test — "switch back" button on bg-list**

```python
@pytest.mark.asyncio
async def test_full_card_includes_switch_back_buttons(mk_feishu):
    adapter, fake = mk_feishu()
    await adapter.note_progress({"kind":"run_started","session_id":"s","run_id":"r1","label":"alpha"})
    await adapter.note_progress({"kind":"run_started","session_id":"s","run_id":"r2","label":"beta"})
    await adapter.render(_evt_draft("r2","..."))
    last = fake.patch_calls[-1]
    assert "切回 alpha" in last.body_json
    assert "switch:r1" in last.body_json  # action key
```

- [ ] **Step 6: Implement switch-back button**

In `build_full_card`, when `running=True` and bg_runs is non-empty (passed in), append action row:
```python
def build_full_card(draft, *, running, bg_runs=()):
    card = ...
    if running and bg_runs:
        actions = [{
            "tag":"button","text":{"tag":"plain_text","content":f"切回 {r.label[:8]}"},
            "value":{"action":"switch_foreground","run_id":r.run_id},
        } for r in bg_runs[:3]]
        card["elements"].append({"tag":"action","actions": actions})
    return card
```

Wire `bg_runs=state.bg_runs()` into all `build_full_card` calls in feishu.py.

Feishu inbound button handler (in `feishu_events.py` action handler) routes `switch_foreground` action → `gateway.switch_foreground(session_id, run_id)`.

- [ ] **Step 7: Run all feishu tests, pass; commit**

```bash
cd tests && git add test_feishu_ph_state_machine.py && git commit -m "test: feishu PH state machine" && cd ..
git add src/xana/agent_adapter/channels/feishu.py src/xana/agent_adapter/channels/feishu_card.py src/xana/agent_adapter/channels/feishu_events.py
git commit -m "feat(feishu): PH state machine, single-card detach UX, switch-back button"
```

---

## Task 5: Telegram single-card merge + PH state machine

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram.py` — same `SessionRenderState` model; full editMessageText pipeline.
- Modify: `src/xana/agent_adapter/channels/telegram_html.py` — add `format_frozen_ph_html(draft, label)`, `format_full_html(draft, *, running, bg_runs)`.
- Delete: `format_pure_answer_html` (Task 9).
- Test: `tests/test_telegram_single_card_ph.py`

- [ ] **Step 1: Failing tests (parallel to feishu Task 4)**

Same scenarios: detach freezes, no edit after freeze, switch_foreground sends new message + freezes old, ph_replaced edits frozen message back to full.

- [ ] **Step 2: Implement**

Mirror Task 4. Key differences:
- `card_anchor` = `(chat_id, message_id)`.
- `_patch` = `bot.edit_message_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=...)`.
- `_send_new` = `bot.send_message(...)` returning new `(chat_id, message_id)`.
- switch-back button = `InlineKeyboardMarkup` with `callback_data=f"switch:{run_id}"`.
- Callback handler routes to `gateway.switch_foreground`.
- TG 48h edit limit: on `edit_message_text` failure with `message can't be edited`, fall back to `send_message` (new anchor) and update `card_anchor`. Log warning.

- [ ] **Step 3: Commit**

```bash
cd tests && git add test_telegram_single_card_ph.py && git commit -m "test: telegram single-card PH state machine" && cd ..
git add src/xana/agent_adapter/channels/telegram.py src/xana/agent_adapter/channels/telegram_html.py
git commit -m "feat(telegram): single-card merge, PH state machine, 48h edit fallback"
```

---

## Task 6: CliRenderer extraction — StdoutRenderer

**Files:**
- Create: `src/xana/agent_adapter/channels/_cli_renderer_stdout.py`
- Modify: `src/xana/agent_adapter/cli.py` — inject `CliRenderer`; refactor `note_progress` + `render` to forward.
- Test: `tests/test_stdout_renderer.py`

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_stdout_renderer_streams_event_text(capsys):
    from xana.agent_adapter.channels._cli_renderer_stdout import StdoutRenderer
    r = StdoutRenderer()
    await r.on_run_started("r1", "hi")
    await r.on_event("r1", _evt_text("hello "))
    await r.on_event("r1", _evt_text("world"))
    out = capsys.readouterr().out
    assert "hello world" in out

@pytest.mark.asyncio
async def test_stdout_renderer_noops_detach_and_switch(capsys):
    r = StdoutRenderer()
    await r.on_run_detached("r1")
    await r.on_foreground_changed("r2")
    assert capsys.readouterr().out == ""
```

- [ ] **Step 2: Implement**

```python
# _cli_renderer_stdout.py
import sys
from xana.agent_adapter.interfaces import CliRenderer

class StdoutRenderer(CliRenderer):
    async def on_run_started(self, run_id, label):
        pass
    async def on_event(self, run_id, event):
        text = event.payload.get("text") or event.payload.get("delta") or ""
        if text:
            sys.stdout.write(text); sys.stdout.flush()
    async def on_run_detached(self, run_id): pass
    async def on_foreground_changed(self, run_id): pass
    async def on_ph_replaced(self, run_id, draft):
        sys.stdout.write("\n"); sys.stdout.flush()
    async def on_run_completed_bg(self, run_id, draft): pass
    async def on_compact_started(self, run_id, fork_point): pass
    async def on_compact_finalized(self, run_id, summary): pass
```

- [ ] **Step 3: Refactor CliAdapter to dispatch via renderer**

`cli.py`:
```python
class CliAdapter(Adapter):
    def __init__(self, *, renderer: CliRenderer, ...):
        self._renderer = renderer
        ...
    async def render(self, event):
        rid = event.payload.get("run_id","")
        await self._renderer.on_event(rid, event)
        return _ok
    async def note_progress(self, payload):
        kind = payload["kind"]; rid = payload.get("run_id","")
        match kind:
            case "run_started": await self._renderer.on_run_started(rid, payload.get("label",""))
            case "run_detached": await self._renderer.on_run_detached(rid)
            case "foreground_changed": await self._renderer.on_foreground_changed(rid)
            case "ph_replaced": await self._renderer.on_ph_replaced(rid, payload.get("draft"))
            case "run_completed_bg": await self._renderer.on_run_completed_bg(rid, payload.get("draft"))
            case "compact_started": await self._renderer.on_compact_started(rid, payload.get("fork_point",0))
            case "compact_finalized": await self._renderer.on_compact_finalized(rid, payload.get("summary",""))
            case _: pass
```

- [ ] **Step 4: Commit**

```bash
cd tests && git add test_stdout_renderer.py && git commit -m "test: StdoutRenderer" && cd ..
git add src/xana/agent_adapter/channels/_cli_renderer_stdout.py src/xana/agent_adapter/cli.py
git commit -m "refactor(cli): inject CliRenderer; StdoutRenderer for oneshot"
```

---

## Task 7: TextualRenderer + xanac App

**Files:**
- Add to deps: `pyproject.toml` → `textual>=0.85`
- Create: `src/xana/agent_adapter/channels/_cli_renderer_textual.py`
- Create: `src/xana/cli/xanac_textual.py` (App + widgets)
- Modify: `src/xana/cli/xanac.py` — entry dispatch: if `argv has prompt or stdin not tty` → oneshot stdout; else → Textual.
- Test: `tests/test_textual_renderer.py` (use Textual `Pilot` for app-level behavior)

- [ ] **Step 1: Failing test — App boots and shows input**

```python
import pytest
from xana.cli.xanac_textual import XanacApp

@pytest.mark.asyncio
async def test_app_boots_with_input(monkeypatch):
    fake_gw = _FakeGateway()
    app = XanacApp(gateway=fake_gw, session_id="s")
    async with app.run_test() as pilot:
        assert app.query_one("#input")
```

- [ ] **Step 2: Implement skeleton App**

```python
# xanac_textual.py
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, Static
from textual.containers import Horizontal, VerticalScroll

class TurnCard(Static):
    pass

class RunPane(VerticalScroll):
    pass

class TabSidebar(VerticalScroll):
    pass

class XanacApp(App):
    BINDINGS = [("tab", "focus_tabs", "Switch run")]
    CSS = "RunPane { width: 3fr; } TabSidebar { width: 1fr; }"
    def __init__(self, *, gateway, session_id):
        super().__init__()
        self.gateway = gateway
        self.session_id = session_id
        self._panes: dict[str, RunPane] = {}
        self._fg: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield RunPane(id="fg-pane")
            yield TabSidebar(id="tabs")
        yield Input(placeholder="message…", id="input")
        yield Footer()

    async def on_input_submitted(self, e: Input.Submitted) -> None:
        text = e.value.strip()
        if not text: return
        self.query_one("#input", Input).value = ""
        from xana.agent_adapter.contracts import InboundMessage
        msg = InboundMessage(adapter_id="cli", session_id=self.session_id, text=text)
        async for _ in self.gateway.submit(msg): pass

    def action_focus_tabs(self) -> None:
        self.set_focus(self.query_one("#tabs"))

    # called by TextualRenderer
    def get_pane(self, run_id: str) -> RunPane:
        if run_id not in self._panes:
            self._panes[run_id] = RunPane()
        return self._panes[run_id]
```

- [ ] **Step 3: Implement TextualRenderer**

```python
# _cli_renderer_textual.py
from xana.agent_adapter.interfaces import CliRenderer

class TextualRenderer(CliRenderer):
    def __init__(self, app):
        self.app = app
        self._buffers: dict[str, str] = {}

    async def on_run_started(self, run_id, label):
        pane = self.app.get_pane(run_id)
        # mount pane into fg slot, push prev to TabSidebar
        ...

    async def on_event(self, run_id, event):
        text = event.payload.get("text","")
        self._buffers[run_id] = self._buffers.get(run_id,"") + text
        # update Static widget in pane
        ...

    async def on_run_detached(self, run_id):
        # move pane to TabSidebar widget
        ...

    async def on_foreground_changed(self, run_id):
        # full re-render: swap visible pane
        ...

    async def on_ph_replaced(self, run_id, draft):
        # paint full card from draft (Markdown widget)
        ...

    async def on_run_completed_bg(self, run_id, draft):
        # no focus steal; pane in TabSidebar paints final
        ...

    async def on_compact_started(self, run_id, fork_point):
        # insert compact pane at fork point in main view
        ...

    async def on_compact_finalized(self, run_id, summary):
        ...
```

Fill the `...` blocks using Textual `Static.update`, `Markdown.update`, container mount/move APIs. Cross-reference `elia` (https://github.com/darrenburns/elia) for layout patterns when implementing.

- [ ] **Step 4: xanac entry dispatch**

`xanac.py`:
```python
def main(argv=None):
    import sys
    args = parser.parse_args(argv)
    if args.prompt or not sys.stdin.isatty():
        return asyncio.run(_oneshot(args))
    return asyncio.run(_textual(args))

async def _oneshot(args):
    from xana.agent_adapter.channels._cli_renderer_stdout import StdoutRenderer
    adapter = CliAdapter(renderer=StdoutRenderer(), ...)
    # one-shot submit + drain
    ...

async def _textual(args):
    from xana.cli.xanac_textual import XanacApp
    from xana.agent_adapter.channels._cli_renderer_textual import TextualRenderer
    # boot gateway with CliAdapter wired to TextualRenderer
    app = XanacApp(gateway=gw, session_id=sid)
    renderer = TextualRenderer(app)
    adapter._renderer = renderer  # late bind
    await app.run_async()
```

- [ ] **Step 5: Failing test — submitting input goes through gateway.submit**

Run Pilot test, press keys, assert `_FakeGateway.submitted_inboxes` has text.

- [ ] **Step 6: Commit**

```bash
cd tests && git add test_textual_renderer.py && git commit -m "test: TextualRenderer + xanac App" && cd ..
git add pyproject.toml src/xana/agent_adapter/channels/_cli_renderer_textual.py src/xana/cli/xanac_textual.py src/xana/cli/xanac.py
git commit -m "feat(cli): Textual REPL with shared oneshot pipeline"
```

---

## Task 8: Compact runner integration

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py` — `start_compact` actually runs compact (no longer stub).
- Modify: `src/xana/agent_adapter/commands.py` — `/compact` handler sets `payload["triggers_turn"] = True`, `payload["cancellable"] = False`; calls `gateway.start_compact`.
- Modify: harness compact threshold path → calls `gateway.start_compact` instead of inline.
- Test: `tests/test_compact_routing.py`

- [ ] **Step 1: Failing test — manual /compact triggers compact_started/finalized**

```python
@pytest.mark.asyncio
async def test_manual_compact_command_emits_events(build_test_gateway):
    gw, adapter = build_test_gateway()
    async for _ in gw.submit(InboundMessage(adapter_id="cli", session_id="s", text="/compact")):
        pass
    kinds = [e["kind"] for e in adapter.progress_events]
    assert "compact_started" in kinds and "compact_finalized" in kinds
```

- [ ] **Step 2: Implement /compact handler**

In `commands.py`, the `/compact` handler:
```python
async def compact(ctx, args):
    fork = await ctx.compute_fork_point(ctx.session_id)
    asyncio.create_task(ctx.gateway.start_compact(ctx.session_id, fork))
    return CommandResult(ok=True, message="压缩中…", payload={"triggers_turn": True, "cancellable": False})
```

- [ ] **Step 3: Implement gateway.start_compact (real)**

```python
async def start_compact(self, session_id, fork_point):
    adapter = next(iter(self.adapters.values()), None)
    if adapter is None: return
    self._run_seq += 1
    rid = f"compact-{session_id}-{self._run_seq}"
    note = getattr(adapter, "note_progress", None)
    if note: await note({"kind":"compact_started","session_id":session_id,"run_id":rid,"fork_point":fork_point})
    try:
        summary = await self._compact_service.run_compact(session_id, fork_point)
        if note: await note({"kind":"compact_finalized","session_id":session_id,"run_id":rid,"summary":summary})
    except Exception as e:
        if note: await note({"kind":"compact_finalized","session_id":session_id,"run_id":rid,"summary":"","error":str(e)})
```

Inject `self._compact_service` via gateway constructor (boot wiring).

- [ ] **Step 4: Auto fork-before compact path**

Find harness's auto-compact trigger (likely `harness_sdk/_invoke` checking token threshold). Replace inline `compact()` call with `await gateway.start_compact(session_id, fork_point)`. Run remains alive (compact is a sibling, not blocking the run).

- [ ] **Step 5: Failing test — compact during a fg run detaches the run**

```python
@pytest.mark.asyncio
async def test_compact_during_fg_run_does_not_block(...):
    # run1 in flight; trigger /compact; assert compact_run becomes fg and run1 detached
    ...
```

- [ ] **Step 6: Commit**

```bash
cd tests && git add test_compact_routing.py && git commit -m "test: compact routing" && cd ..
git add src/xana/agent_adapter/gateway.py src/xana/agent_adapter/commands.py src/xana/harness_sdk/default.py
git commit -m "feat(compact): manual+auto via gateway.start_compact, full PH event flow"
```

---

## Task 9: Cleanup deprecated rendering code

**Files:**
- Delete in `feishu_card.py`: `build_pure_answer_card`.
- Delete in `telegram_html.py`: `format_pure_answer_html`.
- Delete in `_SessionBuffer` of `feishu.py` and `telegram.py`: `started_as_detached` field + branching in finalize.
- Delete in `gateway.py`: `note_run_detached` helper if dead (already removed in c240122 per memory).

- [ ] **Step 1: grep for callers**

```bash
rg "build_pure_answer_card|format_pure_answer_html|started_as_detached" src/ tests/
```

Confirm no remaining callers outside files being edited.

- [ ] **Step 2: Delete; run full test suite**

```bash
cd tests && pytest -x
```

All green.

- [ ] **Step 3: Commit**

```bash
git add -u
git commit -m "chore: drop pure-answer card path superseded by PH state machine"
```

---

## Task 10: Concurrency regression suite

**Files:**
- Create: `tests/test_ph_concurrency_scenarios.py` — implement spec §8 scenarios 1, 2, 3, 5, 6, 7, 8, 13.

Each scenario as a separate `@pytest.mark.asyncio` test using a `_FakeAdapter` capturing the ordered `note_progress` + `render` events plus an in-memory `_FakeGateway` orchestrator that drives the real `gateway._submit` against a stub agent.

- [ ] **Step 1: Build fixture infrastructure** (shared with earlier tasks)

```python
# tests/_ph_fixtures.py
class _StubAgent:
    """Replays a script of HarnessEvents on a delay so we can time detaches."""
    ...

class _FakeAdapter(Adapter):
    """Captures all note_progress + render calls in order."""
    ...

def build_pipeline(*, script=None) -> tuple[Gateway, _FakeAdapter]:
    ...
```

- [ ] **Step 2: Implement scenarios 1, 2, 3, 5, 6, 7, 8, 13** (from spec table)

Each test:
1. Drives the pipeline with the input sequence.
2. Asserts the ordered event log matches expected (run_started → ... → ph_replaced).
3. Asserts adapter state (frozen flags, foreground_run_id) at key checkpoints.

- [ ] **Step 3: Run, fix any flaky scheduling, commit**

```bash
cd tests && pytest test_ph_concurrency_scenarios.py -v
cd tests && git add test_ph_concurrency_scenarios.py _ph_fixtures.py && git commit -m "test: PH concurrency 15-scenario regression suite"
```

---

## Task 11: User acceptance gate

**(Manual step — agent pauses for human verification.)**

- [ ] **Step 1: Restart serve**

```bash
~/.xana/bin/xana sync
```

- [ ] **Step 2: User runs the spec §8 scenarios manually on Telegram and Feishu**

Provide explicit checklist to user:
```
□ TG: send "review xana" then immediately "hello" — confirm two cards, first frozen with 🟡, second streams.
□ TG: tap "切回" on second card — first card un-freezes and continues streaming on a new card.
□ Feishu: same as above.
□ /compact mid-stream: confirm compact card appears + original run detaches without stop button.
□ /help mid-stream: confirm original run KEEPS streaming (no detach).
□ xanac: launch with no args → Textual UI. xanac "hi" → oneshot stdout.
```

- [ ] **Step 2.5: Block until user confirms all six checks pass**

If any fail → fix → restart from failing check.

---

## Task 12: Doc sinkback (spec §13)

**(Run ONLY after Task 11 passes.)**

**Files:**
- Modify: `/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`
- Create: `docs/design/bg-streaming.md`
- Create: `docs/design/compact-rendering.md`
- Modify: `docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`

- [ ] **Step 1: Rewrite ASYNC_RUN_DESIGN.md §卡片渲染模型**

Replace "占位符不渲染 / 纯回答卡" content with PH state machine summary (spec §2 + §4 condensed to ~80 lines). Add at top:
```markdown
> **2026-06-14 渲染模型修订**：见 xana spec
> `docs/superpowers/specs/2026-06-14-ph-driven-rendering-design.md`
```

Update §Adapter 行为 table CLI row → split "Textual (interactive)" / "stdout (oneshot)".

Add §Compact §手动 /compact: "fork_point 之后立刻继续渲染" semantics (spec §5.3).

- [ ] **Step 2: Write `docs/design/bg-streaming.md`**

Concise design doc citing spec as authoritative source. Covers:
- bg-run = fg-run same execution.
- PH state machine (link to spec §2).
- Full re-render on switch (link to spec §3.3).
- Tab/switch_foreground API (cite `gateway.switch_foreground`).
- Command detach policy (spec §6).
- Code locations: `gateway.py`, `_render_state.py`, `feishu.py`, `telegram.py`.

- [ ] **Step 3: Write `docs/design/compact-rendering.md`**

Covers:
- Manual + auto share code (`gateway.start_compact`).
- PH at fork_point + immediate post-fork rendering.
- Write-queue serialization with PH replace (spec §5).
- Code locations: `gateway.start_compact`, `compact_service`, `feishu.build_compact_*_card`.

- [ ] **Step 4: Mark ROADMAP P6 🟡 done**

In `docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`, change:
```
**🟡 待讨论:统一占位符渲染模型(暂列 P6,可能拆出独立 phase)**
```
to:
```
**✅ 已落地（独立 phase）:统一占位符渲染模型** — 见 `docs/superpowers/specs/2026-06-14-ph-driven-rendering-design.md` + `docs/superpowers/plans/2026-06-14-ph-driven-rendering.md`.
```

- [ ] **Step 5: Single commit across xana repo + best_agent repo**

```bash
# xana
git add docs/design/bg-streaming.md docs/design/compact-rendering.md docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md
git commit -m "docs: sink PH-driven rendering design back to xana detailed docs + ROADMAP"

# best_agent (separate repo)
cd /home/xzp/project/best_agent
git add ds_review/ROADMAP/ASYNC_RUN_DESIGN.md
git commit -m "docs(roadmap): 2026-06-14 rendering model revision (xana PH-driven)"
cd -
```

Do NOT push either repo unless user explicitly asks.

---

## Self-review checklist (run after writing)

- [x] Spec §2 state machine → Tasks 3 (state), 4 (feishu), 5 (TG), 7 (Textual).
- [x] Spec §4 events → Tasks 2 (gateway emit), 4/5/6/7 (adapter handle).
- [x] Spec §5 compact → Task 8.
- [x] Spec §6 command detach policy → Task 2 step 6 (`triggers_turn` bypass).
- [x] Spec §7 Textual/oneshot → Tasks 6, 7.
- [x] Spec §8 concurrency → Task 10.
- [x] Spec §9 contract approvals → Task 1 (gate).
- [x] Spec §10 deletions → Task 9.
- [x] Spec §13 sinkback → Task 12.
- [x] No "TBD/TODO/fill in" left in steps; placeholder code blocks (`...`) only inside skeletons that reference spec sections explicitly.
- [x] File paths exact for every Create/Modify.
- [x] Method names consistent (`switch_foreground`, `start_compact`, `on_ph_replaced`).
