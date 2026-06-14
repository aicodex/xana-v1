# Async Chat (PH-First + Dual-Card) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken switch-foreground rendering model with a PH-first single canonical main context + dual-card (fg streaming / bg placeholder fold) UI, unify sync/async on the same data model, and route all main-context writes through a single write-queue consumer.

**Architecture:** Each user submit appends `user_msg + Placeholder` to main under lock and forks a snapshot to the run; runs only ever write their own `runs/{run_id}.jsonl`; a separate write-queue consumer applies `replace_placeholder` / `replace_range` back into main. Rendering uses two card types: at most one **fg streaming card** at a time (with ⏹ Stop / ➡ Background controls at the top), and one lazy-created **bg placeholder fold card** per run that survives detach and absorbs `ph_replaced` and compact summaries.

**Tech Stack:** Python 3.11, asyncio, dataclasses; existing `xana.memory.{contracts,interfaces}`, `xana.agent_adapter.{contracts,interfaces,gateway,channels.feishu,channels.telegram}`, `xana.harness_sdk.default`, `xana.runtime.boot`. Feishu Schema 2.0 cards via `feishu_card.py`. Tests via pytest + pytest-asyncio.

**Source spec:** `docs/superpowers/specs/2026-06-14-async-chat-design.md` (commit 087d7a3).

**Execution mode:** subagent-driven recommended. Each Group is one subagent dispatch; subagent commits at end of group. Group A blocks on user approval of the contract/interface changes before any subagent starts.

---

## Group A — Contract & interface changes (BLOCKING on user approval)

Per `feedback_interfaces_locked.md`: contracts/interfaces are user-approved only. The spec §5 lists the changes; this group makes them and gets a clean import + arch-test pass before any behavior change.

**Files:**
- Modify: `src/xana/agent_adapter/contracts.py`
- Modify: `src/xana/agent_adapter/interfaces.py`
- Modify: `src/xana/memory/interfaces.py`
- Test: `tests/test_arch_layers.py` (must still pass)

### Task A1: Rename `InflightRun.detached` → `is_attached` (inverted semantics)

- [ ] **Step 1: Modify `src/xana/agent_adapter/contracts.py`**

Replace `detached: bool = False` with `is_attached: bool = True`. Update docstring:

```python
@dataclass
class InflightRun:
    """Gateway 侧 per-run 追踪。非 frozen——is_attached 字段可变。
    is_attached=True 表示这是当前 fg run;False = 已 detach 到后台。"""
    session_id: str
    run_id: str
    label: str
    task: _asyncio.Task[Any] | None = None
    is_attached: bool = True
```

- [ ] **Step 2: grep + replace call sites**

Run: `Grep "\.detached" --type py -n`
For each hit, flip the polarity:
- `ir.detached = True` → `ir.is_attached = False`
- `ir.detached = False` → `ir.is_attached = True`
- `if not ir.detached:` → `if ir.is_attached:`
- `if ir.detached:` → `if not ir.is_attached:`

Touch sites: `gateway.py:101-103, 159, 167-179, 197-202`, any test referring to `.detached`.

- [ ] **Step 3: Run arch tests + import smoke**

```bash
pytest tests/test_arch_layers.py -v
python -c "import xana.agent_adapter.gateway, xana.agent_adapter.channels.feishu"
```
Expected: PASS / no ImportError.

### Task A2: Add new gateway abstract methods, rename existing `attach`

The existing `AgentGateway.attach(binding: SessionBinding)` conflicts with the new `attach(session_id, run_id)`. Rename existing → `register_binding`.

- [ ] **Step 1: Modify `src/xana/agent_adapter/interfaces.py`**

Replace `AgentGateway.attach` with:

```python
    @abstractmethod
    async def register_binding(self, binding: SessionBinding) -> None: ...

    @abstractmethod
    async def detach_foreground(self, session_id: str) -> None:
        """Detach current fg run: mark is_attached=False, emit run_detached."""

    @abstractmethod
    async def attach(self, session_id: str, run_id: str) -> None:
        """Auto-detach current fg, then mark target run is_attached=True,
        emit attached(run_id). Caller is responsible for the new fg card."""
```

Delete the `switch_foreground` declaration if present (gateway has it as a concrete method only — no abstract decl to remove).

- [ ] **Step 2: Rename existing `DefaultAgentGateway.attach` → `register_binding`**

In `src/xana/agent_adapter/gateway.py:45-46`:

```python
    async def register_binding(self, binding: SessionBinding) -> None:
        self.bindings[binding.session_id] = binding
```

Leave `detach_foreground` / `attach(session_id, run_id)` as `NotImplementedError` stubs — Group D implements them. Delete `switch_foreground` entirely (will fail at any caller).

- [ ] **Step 3: grep + fix callers of `gateway.attach(binding)`**

Run: `Grep "gateway\.attach\(" --type py -n` and `Grep "\.attach\(binding" --type py -n`. Replace each with `.register_binding(...)`.

Likely sites: `src/xana/runtime/boot.py`, adapter `set_gateway` flows.

- [ ] **Step 4: grep callers of `switch_foreground`**

Run: `Grep "switch_foreground" -n`. List all hits to the user — these all need rewrites in Group D / E (tests + adapter render paths). For now: delete the gateway method, leave call sites broken; Group D rewrites them.

- [ ] **Step 5: Run arch tests**

```bash
pytest tests/test_arch_layers.py -v
```
Expected: PASS.

### Task A3: Add `replace_range` to memory interfaces

- [ ] **Step 1: Modify `src/xana/memory/interfaces.py`**

`ActiveContextStore` already has `append_placeholder` / `replace_placeholder` / `snapshot`. Add:

```python
    @abstractmethod
    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        msg_ids: tuple[str, ...],
        summary_message: MemoryMessage,
    ) -> ActiveContext | None: ...
```

(`msg_ids` are the IDs in the original snapshot that the compact summary replaces. Since current `MemoryMessage` is frozen w/o an id field, use indices instead — change signature to `(start_idx: int, end_idx: int)`.)

Final signature:

```python
    @abstractmethod
    async def replace_range(
        self,
        session_id: str,
        agent_name: str,
        start_idx: int,
        end_idx: int,
        summary_message: MemoryMessage,
    ) -> ActiveContext | None: ...
```

`HistoryMirror` add the same `replace_range(...)` plus mirror methods for placeholder ops (if missing). Check current `HistoryMirror` ABC — if only `append`/`replay` exist, add:

```python
    @abstractmethod
    async def append_placeholder(self, session_id: str, agent_name: str, placeholder: Placeholder) -> None: ...

    @abstractmethod
    async def replace_placeholder(self, run_id: str, messages: tuple[MemoryMessage, ...]) -> None: ...

    @abstractmethod
    async def replace_range(
        self, session_id: str, agent_name: str, start_idx: int, end_idx: int,
        summary_message: MemoryMessage,
    ) -> None: ...
```

- [ ] **Step 2: Stub implementations in concrete classes**

Find implementations: `Grep "class.*ActiveContextStore" --type py` and `Grep "class.*HistoryMirror" --type py`. For each non-test impl, add `raise NotImplementedError` stubs for new methods. Group B/C/H wire real logic.

- [ ] **Step 3: arch tests**

```bash
pytest tests/test_arch_layers.py -v
```
Expected: PASS.

### Task A4: Commit Group A

- [ ] **Step 1: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(contracts): rename InflightRun.detached→is_attached + add detach_foreground/attach/replace_range

Spec: docs/superpowers/specs/2026-06-14-async-chat-design.md §5.

- InflightRun.detached → is_attached (inverted)
- AgentGateway.attach(binding) → register_binding(binding)
- AgentGateway adds detach_foreground(sid), attach(sid, run_id) (stubs)
- AgentGateway.switch_foreground deleted (callers in Group D)
- ActiveContextStore + HistoryMirror add replace_range; HistoryMirror
  gains append_placeholder / replace_placeholder for parity

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Group B — main context PH writes + snapshot fork

The submit flow becomes: under lock → snapshot → append user_msg → append PH → enqueue fork_ctx (the snapshot + user_msg). The prompt assembler must expand `Placeholder` nodes (from other runs) into synthetic user/assistant pairs before sending to the LLM.

**Files:**
- Modify: `src/xana/memory/active_context.py` (or wherever the default `ActiveContextStore` lives — `Grep "class.*\(ActiveContextStore\)"` to confirm)
- Modify: `src/xana/agents/chat_agent.py` (submit flow: replace direct `append_message` with snapshot+PH ritual)
- Modify: `src/xana/harness_sdk/prompt_assembler.py` (or equivalent — `Grep "_assemble_prompt\|PromptAssembler"`)
- Test: `tests/test_active_context_ph.py` (new), `tests/test_prompt_assembler_ph_expand.py` (new)

### Task B1: ActiveContextStore: implement snapshot + append_placeholder + replace_placeholder semantics

Likely already partially implemented (per spec §1.2 noting `Placeholder` was added in P2). Audit current state.

- [ ] **Step 1: Audit existing implementation**

Run: `Grep "append_placeholder\|replace_placeholder\|snapshot" src/xana/memory/ -n`. Read the concrete class.

- [ ] **Step 2: Write tests for snapshot mixed sequence**

`tests/test_active_context_ph.py`:

```python
import pytest
from xana.memory.contracts import MemoryMessage, Placeholder
from xana.memory.active_context import DefaultActiveContextStore  # adjust import

@pytest.mark.asyncio
async def test_snapshot_returns_mixed_sequence(tmp_path):
    store = DefaultActiveContextStore(root=tmp_path)
    sid, agent = "s1", "chat"
    await store.append_message(sid, agent, MemoryMessage(role="user", content="hi"))
    await store.append_placeholder(sid, agent, Placeholder(run_id="r1", label="hi"))
    await store.append_message(sid, agent, MemoryMessage(role="user", content="next"))
    snap = await store.snapshot(sid, agent)
    assert len(snap) == 3
    assert isinstance(snap[0], MemoryMessage)
    assert isinstance(snap[1], Placeholder) and snap[1].run_id == "r1"
    assert isinstance(snap[2], MemoryMessage)

@pytest.mark.asyncio
async def test_replace_placeholder_swaps_in_messages(tmp_path):
    store = DefaultActiveContextStore(root=tmp_path)
    sid, agent = "s1", "chat"
    await store.append_placeholder(sid, agent, Placeholder(run_id="r1", label="x"))
    msgs = (MemoryMessage(role="assistant", content="done"),)
    await store.replace_placeholder("r1", msgs)
    snap = await store.snapshot(sid, agent)
    assert len(snap) == 1
    assert isinstance(snap[0], MemoryMessage) and snap[0].content == "done"
```

- [ ] **Step 3: Run tests — expected FAIL until impl matches**

```bash
pytest tests/test_active_context_ph.py -v
```

- [ ] **Step 4: Implement / fix store methods**

In the concrete store, maintain mixed list internally. `snapshot` returns tuple. `replace_placeholder` swaps `messages[idx:idx+1] = list(replace_with)`. Hold session lock for all mutations + snapshot.

- [ ] **Step 5: Run tests → PASS**

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(memory): snapshot/append_placeholder/replace_placeholder default impl + tests"
```

### Task B2: ActiveContextStore.replace_range

- [ ] **Step 1: Test**

```python
@pytest.mark.asyncio
async def test_replace_range_collapses_to_summary(tmp_path):
    store = DefaultActiveContextStore(root=tmp_path)
    sid, agent = "s1", "chat"
    for i in range(4):
        await store.append_message(sid, agent, MemoryMessage(role="user", content=f"m{i}"))
    summary = MemoryMessage(role="compact", content="summary of m0-m2")
    await store.replace_range(sid, agent, 0, 3, summary)
    snap = await store.snapshot(sid, agent)
    assert len(snap) == 2
    assert snap[0].content == "summary of m0-m2"
    assert snap[1].content == "m3"
```

- [ ] **Step 2: Implement**

```python
async def replace_range(self, sid, agent, start_idx, end_idx, summary_message):
    async with self._lock_for(sid, agent):
        msgs = self._messages_for(sid, agent)
        msgs[start_idx:end_idx] = [summary_message]
        self._touch(sid, agent)
        return await self.get(sid, agent)
```

- [ ] **Step 3: Run → PASS. Commit.**

### Task B3: chat_agent.submit ritual — snapshot + append user + append PH + enqueue fork_ctx

The current `ChatAgent.send` probably appends user_msg → builds prompt → invokes harness directly. We change it to: under lock, take snapshot, append user_msg, append PH; then build the harness invocation against `snapshot + [user_msg]` (the fork_ctx). The PH is in main but NOT in the run's working view.

- [ ] **Step 1: Read current chat_agent.send**

Run: `Read src/xana/agents/chat_agent.py` and find `send()` + `to_harness_context()`.

- [ ] **Step 2: Locate where ActiveContext is read into HarnessContext**

Likely in `to_harness_context` or harness invocation. Identify the exact line that reads `active_context.messages`.

- [ ] **Step 3: Test (integration-style)**

`tests/test_chat_agent_ph_submit.py`:

```python
@pytest.mark.asyncio
async def test_submit_appends_user_then_ph_to_main(chat_agent_fixture):
    ca = chat_agent_fixture
    sid = "s1"
    request = make_request(sid, "hello")
    async for _ in ca.send(request):
        break  # only need to verify post-submit state
    snap = await ca._active_context_store.snapshot(sid, "chat")
    assert len(snap) >= 2
    user = next(m for m in snap if not hasattr(m, "run_id") and m.role == "user")
    ph = next(m for m in snap if hasattr(m, "run_id"))
    assert user.content == "hello"
    assert ph.label.startswith("hello")
    # PH appears AFTER its user_msg (immediate succession)
    idx_user = list(snap).index(user)
    idx_ph = list(snap).index(ph)
    assert idx_ph == idx_user + 1
```

- [ ] **Step 4: Implement in chat_agent.send**

```python
async def send(self, request):
    sid = request.session_id
    run_id = str(request.metadata.get("gateway_run_id") or
                 f"{sid}-{uuid.uuid4().hex[:8]}")
    label = (request.user_input or "").strip()[:40] or "run"
    user_msg = MemoryMessage(role="user", content=request.user_input)

    # PH-first ritual: snapshot + append user + append PH, atomically.
    snap = await self._active_context_store.snapshot_then_append(
        sid, "chat",
        [user_msg, Placeholder(run_id=run_id, label=label, status="running",
                               jsonl_path=Path(f"runs/{run_id}.jsonl"))],
    )
    # snap is the pre-append snapshot — run's fork view = snap + [user_msg]
    fork_ctx = tuple(snap) + (user_msg,)
    harness_ctx = await self.to_harness_context(request, fork_ctx=fork_ctx, run_id=run_id)
    async for event in self._harness.invoke(harness_ctx):
        yield event
```

The new store method `snapshot_then_append(sid, agent, items)` atomically takes a snapshot before applying the appends and returns it. Add to ActiveContextStore ABC if not present — **this is an interface change → roll into Task A3 retroactively** (mark TODO, plan revision).

> ⚠️ Plan revision note: adding `snapshot_then_append` to ABC. Either fold into Group A (preferred) or implement as a default mixin that locks + calls existing methods. Recommend the latter for this plan: add as non-abstract helper on the ABC using `snapshot` + `append_message` + `append_placeholder` under `self._session_lock(sid)`.

- [ ] **Step 5: Wire `to_harness_context` to take fork_ctx instead of reading from store**

Change signature: `to_harness_context(request, fork_ctx, run_id)`. Pass fork_ctx straight into HarnessContext (do NOT re-read active context).

- [ ] **Step 6: Tests pass. Commit.**

### Task B4: Prompt assembler — expand Placeholder into synthetic user/assistant pair

When the harness assembles the LLM prompt from fork_ctx, any `Placeholder` in the sequence becomes two synthetic turns: `user: {ph.label}` followed by `assistant: [此问题正在后台处理中,请基于其他可用上下文继续。]`.

- [ ] **Step 1: Find prompt assembler**

Run: `Grep "PromptAssembler\|_assemble_prompt\|def assemble" src/xana/harness_sdk -n`.

- [ ] **Step 2: Test**

`tests/test_prompt_assembler_ph_expand.py`:

```python
from xana.memory.contracts import MemoryMessage, Placeholder
from xana.harness_sdk.prompt_assembler import expand_placeholders  # to be added

def test_expand_placeholder_yields_synthetic_pair():
    msgs = (
        MemoryMessage(role="user", content="A"),
        Placeholder(run_id="r1", label="review xana", status="running"),
        MemoryMessage(role="user", content="B"),
    )
    out = expand_placeholders(msgs)
    assert [m.role for m in out] == ["user", "user", "assistant", "user"]
    assert out[1].content == "review xana"
    assert "后台处理中" in out[2].content
    assert out[3].content == "B"
```

- [ ] **Step 3: Implement `expand_placeholders`**

```python
_PH_ASSISTANT = "[此问题正在后台处理中,请基于其他可用上下文继续。]"

def expand_placeholders(msgs):
    out = []
    for m in msgs:
        if isinstance(m, Placeholder):
            out.append(MemoryMessage(role="user", content=m.label))
            out.append(MemoryMessage(role="assistant", content=_PH_ASSISTANT))
        else:
            out.append(m)
    return tuple(out)
```

- [ ] **Step 4: Wire into assembler — call `expand_placeholders` on fork_ctx before sending to LLM**

Add the call at the assembler entry where it currently iterates `context.messages`. Skip if no placeholders (fast path).

- [ ] **Step 5: Tests pass. Commit.**

```bash
git commit -am "feat(harness): expand Placeholder into synthetic user/assistant in prompt"
```

---

## Group C — Write queue + single consumer

Runs write `runs/{run_id}.jsonl`. When run finishes (or compact finishes), an item is pushed to a session-wide write_queue. A single consumer task (started by `boot`) drains the queue and applies `replace_placeholder` / `replace_range` to main under lock.

**Files:**
- Create: `src/xana/memory/write_queue.py`
- Modify: `src/xana/runtime/boot.py` (start consumer task, expose `write_queue` on BootResult)
- Modify: `src/xana/agents/chat_agent.py` (on run completion → enqueue replace)
- Test: `tests/test_write_queue.py`

### Task C1: WriteQueue + WriteQueueConsumer

- [ ] **Step 1: Test**

`tests/test_write_queue.py`:

```python
import asyncio, pytest
from xana.memory.contracts import MemoryMessage
from xana.memory.write_queue import WriteQueue, WriteQueueConsumer, ReplaceItem

@pytest.mark.asyncio
async def test_replace_item_applied_to_store(active_context_store):
    sid, agent = "s1", "chat"
    await active_context_store.append_placeholder(sid, agent, Placeholder(run_id="r1", label="x"))
    wq = WriteQueue()
    consumer = WriteQueueConsumer(wq, store=active_context_store, mirror=None)
    task = asyncio.create_task(consumer.run())
    msgs = (MemoryMessage(role="assistant", content="done"),)
    await wq.put(ReplaceItem(run_id="r1", session_id=sid, agent_name=agent, messages=msgs))
    await asyncio.sleep(0.05)
    snap = await active_context_store.snapshot(sid, agent)
    assert len(snap) == 1 and snap[0].content == "done"
    consumer.stop()
    await task
```

- [ ] **Step 2: Implement `write_queue.py`**

```python
from __future__ import annotations
import asyncio, contextlib
from dataclasses import dataclass
from xana.memory.contracts import MemoryMessage
from xana.memory.interfaces import ActiveContextStore, HistoryMirror


@dataclass(frozen=True)
class ReplaceItem:
    run_id: str
    session_id: str
    agent_name: str
    messages: tuple[MemoryMessage, ...]


@dataclass(frozen=True)
class ReplaceRangeItem:
    session_id: str
    agent_name: str
    start_idx: int
    end_idx: int
    summary: MemoryMessage


class WriteQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
    async def put(self, item) -> None:
        await self._q.put(item)
    async def get(self):
        return await self._q.get()


class WriteQueueConsumer:
    def __init__(self, queue: WriteQueue, *,
                 store: ActiveContextStore, mirror: HistoryMirror | None) -> None:
        self._q = queue
        self._store = store
        self._mirror = mirror
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            await self._apply(item)

    async def _apply(self, item) -> None:
        if isinstance(item, ReplaceItem):
            await self._store.replace_placeholder(item.run_id, item.messages)
            if self._mirror is not None:
                await self._mirror.replace_placeholder(item.run_id, item.messages)
        elif isinstance(item, ReplaceRangeItem):
            await self._store.replace_range(
                item.session_id, item.agent_name,
                item.start_idx, item.end_idx, item.summary,
            )
            if self._mirror is not None:
                await self._mirror.replace_range(
                    item.session_id, item.agent_name,
                    item.start_idx, item.end_idx, item.summary,
                )

    def stop(self) -> None:
        self._stopped.set()
```

- [ ] **Step 3: Tests pass. Commit.**

### Task C2: boot wires write queue + consumer (independent task)

- [ ] **Step 1: Modify boot**

In `src/xana/runtime/boot.py`, after creating `active_context_store` + `history_mirror`:

```python
from xana.memory.write_queue import WriteQueue, WriteQueueConsumer

write_queue = WriteQueue()
write_consumer = WriteQueueConsumer(write_queue,
                                    store=active_context_store, mirror=history_mirror)
write_consumer_task = asyncio.create_task(write_consumer.run())
```

Add `write_queue`, `write_consumer`, `write_consumer_task` to `BootResult`. Ensure `boot` registers `write_consumer.stop()` on shutdown.

> ⚠️ **CRITICAL** (spec §11): write_queue consumer must be a SEPARATE asyncio task from the run consumer. Sync mode otherwise deadlocks (run consumer awaits `_invoke`, `_invoke` enqueues, no one drains).

- [ ] **Step 2: Smoke test**

```bash
pytest tests/test_boot_smoke.py -v   # or equivalent
```
Expected: PASS (boot still starts; new consumer task running).

- [ ] **Step 3: Commit**

### Task C3: chat_agent enqueues ReplaceItem on run completion

- [ ] **Step 1: Identify completion point in chat_agent.send loop**

The harness yields final events; on `event.kind == "final"` (or cancelled / error), we need to:
1. Read `runs/{run_id}.jsonl` (history of this run's messages, minus completion marker)
2. Convert to `tuple[MemoryMessage, ...]`
3. `await write_queue.put(ReplaceItem(...))`

- [ ] **Step 2: Add `runs_jsonl_to_memory_messages(path) -> tuple[MemoryMessage,...]` helper**

`src/xana/memory/runs_loader.py`:

```python
import json
from pathlib import Path
from xana.memory.contracts import MemoryMessage

def runs_jsonl_to_memory_messages(path: Path) -> tuple[MemoryMessage, ...]:
    out = []
    if not path.exists():
        return ()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "run_completed":
            continue
        if row.get("role") and row.get("content") is not None:
            out.append(MemoryMessage(role=row["role"], content=row["content"],
                                     metadata=row.get("metadata", {})))
    return tuple(out)
```

- [ ] **Step 3: Test the loader**

`tests/test_runs_loader.py` with sample jsonl. Run + commit.

- [ ] **Step 4: In chat_agent.send finally block, enqueue replace**

```python
finally:
    msgs = runs_jsonl_to_memory_messages(Path(self._run_jsonl_path(run_id)))
    await self._write_queue.put(ReplaceItem(
        run_id=run_id, session_id=sid, agent_name="chat", messages=msgs,
    ))
```

(Inject `write_queue` via ChatAgent constructor; thread through boot.)

- [ ] **Step 5: Integration test**

`tests/test_chat_agent_ph_replacement.py`:

```python
@pytest.mark.asyncio
async def test_run_completion_replaces_ph_in_main(boot_fixture):
    # submit a fake-tool prompt; wait for completion; assert snap has the run's
    # messages where the PH used to be.
```

- [ ] **Step 6: Commit**

---

## Group D — Gateway detach_foreground / attach

Replaces `switch_foreground`. Used by adapters when user sends a new message (auto-detach), clicks ➡ Background, or uses `/attach`.

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py`
- Test: `tests/test_gateway_detach_attach.py`

### Task D1: implement detach_foreground

- [ ] **Step 1: Test**

```python
@pytest.mark.asyncio
async def test_detach_foreground_marks_and_emits(gateway, fake_adapter, fake_inflight):
    sid = "s1"
    gateway._inflight[sid] = [fake_inflight(run_id="r1", is_attached=True)]
    await gateway.detach_foreground(sid)
    assert gateway._inflight[sid][0].is_attached is False
    kinds = [p["kind"] for p in fake_adapter.progress]
    assert "run_detached" in kinds
```

- [ ] **Step 2: Implement**

```python
async def detach_foreground(self, session_id: str) -> None:
    for ir in self._inflight.get(session_id, []):
        if ir.is_attached:
            ir.is_attached = False
            for adapter in self.adapters.values():
                self._notify_run_detached(adapter, session_id, ir)
            return
```

- [ ] **Step 3: Replace `detach_inflight_for` call sites with `detach_foreground`** (or keep the old as alias). Drop the old.

- [ ] **Step 4: Update `_submit` auto-detach path** (lines 99-103) — call `await self.detach_foreground(inbound.session_id)` instead of inline loop. Note this happens synchronously before creating new_run.

- [ ] **Step 5: PASS + commit**

### Task D2: implement attach(session_id, run_id)

- [ ] **Step 1: Test**

```python
@pytest.mark.asyncio
async def test_attach_detaches_others_and_emits_attached(gateway, fake_adapter):
    sid = "s1"
    gateway._inflight[sid] = [
        InflightRun(sid, "r1", "a", is_attached=True),
        InflightRun(sid, "r2", "b", is_attached=False),
    ]
    await gateway.attach(sid, "r2")
    assert gateway._inflight[sid][0].is_attached is False  # r1 detached
    assert gateway._inflight[sid][1].is_attached is True   # r2 attached
    kinds = [(p["kind"], p.get("run_id")) for p in fake_adapter.progress]
    assert ("run_detached", "r1") in kinds
    assert ("attached", "r2") in kinds

@pytest.mark.asyncio
async def test_attach_unknown_runid_is_noop(gateway, fake_adapter):
    await gateway.attach("s1", "missing")
    assert fake_adapter.progress == []
```

- [ ] **Step 2: Implement**

```python
async def attach(self, session_id: str, run_id: str) -> None:
    runs = self._inflight.get(session_id, [])
    target = next((ir for ir in runs if ir.run_id == run_id), None)
    if target is None:
        return
    # auto-detach current fg(s)
    for ir in runs:
        if ir.run_id != run_id and ir.is_attached:
            ir.is_attached = False
            for ad in self.adapters.values():
                self._notify_run_detached(ad, session_id, ir)
    target.is_attached = True
    for ad in self.adapters.values():
        note = getattr(ad, "note_progress", None)
        if note is not None:
            note({"kind": "attached", "session_id": session_id, "run_id": run_id,
                  "label": target.label})
```

- [ ] **Step 3: PASS + commit**

### Task D3: delete `switch_foreground` + `start_compact` stub

- [ ] **Step 1: Delete the methods** from `gateway.py` (lines 181-211).
- [ ] **Step 2: Grep all callers; fix or note**

Callers of `switch_foreground` likely live in test fixtures + the obsolete `test_feishu_race_e2e.py::test_scenario_c_switch_back_mid_flight`. That test will be rewritten in Group K. For now: comment out / xfail.

Callers of `start_compact`: probably command_router for `/compact`. The real compact (Group H) replaces it; for now mark TODO.

- [ ] **Step 3: Commit**

---

## Group E — Feishu PH-first dual-card rendering

The biggest visual change. Replace current "single growing card with bg_runs buttons" model with: fg streaming card (with ⏹/➡ top controls) + lazy-created bg placeholder fold card per detached run.

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu_card.py` (new card builder; delete `bg_runs` kwarg + `build_completed_bg_card`)
- Modify: `src/xana/agent_adapter/channels/feishu.py` (render state machine: fg msg + bg msg per run, lazy bg)
- Test: `tests/test_feishu_dual_card.py`

### Task E1: card builders

- [ ] **Step 1: Add `build_fg_streaming_card(draft, *, finalize)` to feishu_card.py**

Top controls (column_set with 2 buttons: ⏹ 停止 / ➡ 后台) — emit `action: "stop"` / `action: "detach_foreground"` in `behaviors[].value`. Body = existing `build_card` body. Skip top controls if `finalize=True` (card frozen / completed).

```python
_FG_TOP_CONTROLS = {
    "tag": "column_set", "horizontal_spacing": "8px",
    "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "⏹ 停止"},
            "type": "default", "size": "medium",
            "behaviors": [{"type": "callback",
                           "value": {"action": "stop_foreground"}}],
        }]},
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "➡ 后台"},
            "type": "default", "size": "medium",
            "behaviors": [{"type": "callback",
                           "value": {"action": "detach_foreground"}}],
        }]},
    ],
}

def build_fg_streaming_card(draft, *, finalize: bool = False) -> dict:
    card = build_card(draft, finalize=finalize)
    if not finalize:
        card["body"]["elements"].insert(0, _FG_TOP_CONTROLS)
    return card
```

- [ ] **Step 2: Add `build_bg_placeholder_card(...)`**

```python
def build_bg_placeholder_card(
    *,
    label: str,
    state: str,                # "running" | "compacting" | "completed" | "cancelled" | "failed"
    progress_pct: int,         # 0..100
    panel_md: str,             # content for the collapsible block
    compacted_n: int = 0,
) -> dict:
    titles = {
        "running": f"🟡 正在后台 · {label}",
        "compacting": f"🌀 正在压缩… · {label}",
        "completed": f"✅ 回答完成 · {label}",
        "cancelled": f"🛑 已取消 · {label}",
        "failed": f"✖ 失败 · {label}",
    }
    title = titles.get(state, f"· {label}")
    if compacted_n > 0 and state in ("running", "completed"):
        title = f"已压缩 {compacted_n} 条 · " + title
    bar = "▰" * (progress_pct // 10) + "▱" * (10 - progress_pct // 10)
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [
            {"tag": "markdown",
             "content": f"<font color='grey'>{bar} {progress_pct}%</font>"},
            _panel("详情", panel_md),
        ]},
    }
```

- [ ] **Step 3: Delete `build_completed_bg_card` + `bg_runs` kwarg from `build_card`**

Remove the entire `if bg_runs:` block in `build_card`. Remove `build_completed_bg_card`. Adjust callers (Group K cleans up references).

- [ ] **Step 4: Tests**

```python
def test_fg_streaming_has_top_controls():
    card = build_fg_streaming_card(draft_factory("hi"))
    first = card["body"]["elements"][0]
    assert first["tag"] == "column_set"
    actions = [c["elements"][0]["behaviors"][0]["value"]["action"]
               for c in first["columns"]]
    assert actions == ["stop_foreground", "detach_foreground"]

def test_bg_placeholder_progress_bar_title():
    card = build_bg_placeholder_card(label="hi", state="running",
                                     progress_pct=40, panel_md="x")
    assert "🟡" in card["header"]["title"]["content"]
    bar_md = card["body"]["elements"][0]["content"]
    assert "40%" in bar_md
    assert any(el.get("tag") == "collapsible_panel"
               for el in card["body"]["elements"])

def test_bg_completed_title():
    card = build_bg_placeholder_card(label="hi", state="completed",
                                     progress_pct=100, panel_md="answer")
    assert "✅" in card["header"]["title"]["content"]
```

- [ ] **Step 5: PASS + commit**

### Task E2: feishu adapter dual-card state machine

`SessionRenderState` (in `feishu.py`) currently tracks one fg message_id per session. Now it must track:
- `fg_run_id: str | None` — current fg
- `fg_message_id: str | None` — current fg card
- `bg_cards: dict[run_id, str]` — message_id per bg card (lazy created)
- `bg_state: dict[run_id, BgCardState]` — title state, progress_pct, panel_md

- [ ] **Step 1: Read current `feishu.py` render state**

Identify `_render_states`, `SessionRenderState`, `RunRenderState`, `_make_send`, `render`, `note_progress`.

- [ ] **Step 2: Add state**

```python
@dataclass
class BgCardState:
    message_id: str
    label: str
    state: str = "running"      # see build_bg_placeholder_card
    progress_pct: int = 0
    panel_md: str = ""
    compacted_n: int = 0

# in SessionRenderState:
fg_run_id: str | None = None
fg_message_id: str | None = None
bg_cards: dict[str, BgCardState] = field(default_factory=dict)
```

- [ ] **Step 3: note_progress dispatch table**

| kind | action |
|---|---|
| `run_started` | set state.fg_run_id = run_id (lazy fg message — created on first event) |
| `run_detached` | freeze fg msg (patch as `finalize=True` w/o controls); lazy-create bg card if not in `bg_cards`; clear `fg_*` |
| `attached` | freeze previous fg (if any); set fg_run_id = new run_id; reset fg_message_id None (next event creates new card); bg card untouched |
| `ph_replaced` | if bg card exists for run_id: patch bg card with state="completed", panel_md = expanded run history, progress_pct=100; if still fg (never detached): nothing (fg card finalizes via coalescer) |
| `compact_started(run_id, fork_point)` | if bg card exists for affected run_id: patch with state="compacting", progress_pct=0; else lazy-create a case-B "compact-only" bg card with no panel |
| `compact_progress(run_id, pct)` | update progress_pct |
| `compact_finalized(run_id, summary)` | progress_pct=100; prepend summary to panel_md; state back to "running" or stay "completed"; `compacted_n += 1` |
| `usage_progress(run_id, ...)` | route by run_id; only if run_id is current fg, kick coalescer |
| (frozen run events) | swallow — no patch |

- [ ] **Step 4: Test scaffolding (use fake sender)**

```python
@pytest.mark.asyncio
async def test_detach_freezes_fg_and_creates_bg_card(adapter, fake_sender):
    sid = "s1"; rid = "r1"
    adapter.note_progress({"kind":"run_started","session_id":sid,"run_id":rid,"label":"hi"})
    # stream some events to create fg card
    await adapter.render(make_streaming_event(rid, "hello"))
    assert len(fake_sender.sends) == 1  # fg msg created
    adapter.note_progress({"kind":"run_detached","session_id":sid,"run_id":rid,"label":"hi"})
    # fg patched to frozen + bg card sent
    assert any("🟡 正在后台" in (c.get("header") or {}).get("title", {}).get("content", "")
               for _, c in fake_sender.sends)
    # subsequent events on rid (now detached) do NOT patch fg
    pre = len(fake_sender.patches)
    await adapter.render(make_streaming_event(rid, "ignored"))
    assert len(fake_sender.patches) == pre  # no new patches

@pytest.mark.asyncio
async def test_ph_replaced_completes_bg_card(adapter, fake_sender):
    # setup: rid was detached, bg card exists
    ...
    adapter.note_progress({"kind":"ph_replaced","session_id":sid,"run_id":rid,"mode":"full"})
    last_bg = [c for _, c in fake_sender.patches
               if "回答完成" in (c.get("header") or {}).get("title", {}).get("content", "")]
    assert last_bg
```

- [ ] **Step 5: Implement note_progress + render dispatch**

Bulk rewrite of `note_progress` + `_make_send` (and `_send_full_card`). Drop the old switch_back / bg_runs logic. Each note_progress branch above becomes a small method on FeishuAdapter that schedules api_sender calls via `asyncio.create_task` so note_progress stays non-blocking.

- [ ] **Step 6: Tests PASS + commit**

### Task E3: callback handling — ⏹ Stop / ➡ Background buttons

When user clicks the fg-card buttons, Feishu sends a callback event with the `value` payload. Route to gateway methods.

- [ ] **Step 1: Find callback dispatcher in feishu.py**

Run: `Grep "action.*switch_foreground\|callback.*value" src/xana/agent_adapter/channels/feishu.py -n`. There's an existing callback handler that processed switch_foreground.

- [ ] **Step 2: Update dispatcher**

```python
async def _handle_card_callback(self, sid: str, value: dict) -> None:
    action = value.get("action")
    if action == "stop_foreground":
        # find current fg run
        runs = self._gateway._inflight.get(sid, [])
        fg = next((ir for ir in runs if ir.is_attached), None)
        if fg:
            await self._gateway.cancel(sid, fg.run_id)
    elif action == "detach_foreground":
        await self._gateway.detach_foreground(sid)
    elif action == "attach_run":
        rid = value.get("run_id")
        if rid:
            await self._gateway.attach(sid, rid)
```

(The `attach_run` action is for /attach menu — Group I.)

- [ ] **Step 3: Tests + commit**

---

## Group F — Telegram dual-card (single-message edit model)

TG can't have two persistent cards as easily. Per spec §8: bg card in TG = a single text message with "🟡 正在后台" header that gets edited the same way Feishu patches a card.

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram.py`
- Test: `tests/test_telegram_dual_card.py`

### Task F1: TG bg placeholder = headed text message

- [ ] **Step 1: Add `render_bg_text(...)` helper** that returns the same content shape as `build_bg_placeholder_card` but as Markdown:

```python
def render_bg_text(*, label, state, progress_pct, panel_md, compacted_n=0) -> str:
    head = {"running":"🟡 正在后台", "compacting":"🌀 正在压缩…",
            "completed":"✅ 回答完成", "cancelled":"🛑 已取消", "failed":"✖ 失败"}[state]
    if compacted_n:
        head = f"已压缩 {compacted_n} 条 · " + head
    bar = "▰" * (progress_pct // 10) + "▱" * (10 - progress_pct // 10)
    return f"*{head} · {label}*\n{bar} {progress_pct}%\n\n<details>\n{panel_md}\n</details>"
```

- [ ] **Step 2: Add state machine (mirror of E2) to telegram adapter**: lazy-create bg message on first detach; track `bg_messages: dict[run_id, message_id]`; edit on ph_replaced / compact events.

- [ ] **Step 3: Tests using fake TG sender — patch list audit + commit**

### Task F2: TG fg controls

TG inline keyboard buttons under the fg message: `[⏹ 停止] [➡ 后台]` with callback_data carrying action. On detach, remove keyboard (edit message reply_markup to None).

- [ ] **Step 1: Implement keyboard generation + callback handler**
- [ ] **Step 2: Tests + commit**

---

## Group G — CLI / Textual dual-view

xanac REPL: keep fg as the active streaming print; on detach, mark the run as collapsed bg entry (stdout: a single line "🟡 run-X 后台运行中"; on ph_replaced, dump the answer prefixed with "✅ run-X").

**Files:**
- Modify: `src/xana/agent_adapter/channels/console.py` (or `stdout_renderer.py`)
- Modify: `src/xana/agent_adapter/channels/textual_renderer.py` (if separate)

### Task G1: StdoutRenderer detach handling

- [ ] **Step 1: Implement on_run_detached**: print one line "🟡 后台运行中 · {label}" and stop forwarding events for that run_id (filter by run_id in on_event).
- [ ] **Step 2: Implement on_ph_replaced**: print "✅ 已完成 · {label}\n{summary}".
- [ ] **Step 3: Tests via captured stdout + commit**

### Task G2: TextualRenderer pane

- [ ] **Step 1: Add a "Background" pane that lists detached runs with their state + percentage**. Update on note_progress events.
- [ ] **Step 2: Manual smoke test (Textual is interactive). Document in plan: "validated manually by running `xana chat`".**
- [ ] **Step 3: Commit**

---

## Group H — Compact via write queue, case A / case B

Compact runs through harness, produces a summary, and lands on a bg card (case A: existing PH; case B: new bg card).

**Files:**
- Modify: harness compact path (Grep `compact_started`, `compact_finalized` emissions)
- Modify: `src/xana/agent_adapter/gateway.py` (delete stub `start_compact`)
- Modify: feishu/telegram adapters: route compact events to bg cards
- Test: `tests/test_compact_dual_card.py`

### Task H1: real compact through harness + replace_range via write queue

- [ ] **Step 1: Locate current compact implementation**

Run: `Grep "compact_started\|class.*Compact" src/xana/harness_sdk -n`.

- [ ] **Step 2: Wire it to emit msg_range + summary as `ReplaceRangeItem` to write_queue on finalize**

```python
await write_queue.put(ReplaceRangeItem(
    session_id=sid, agent_name="chat",
    start_idx=0, end_idx=fork_point,
    summary=MemoryMessage(role="compact", content=summary),
))
```

- [ ] **Step 3: Test**

`tests/test_compact_dual_card.py::test_compact_replaces_range_in_main`.

- [ ] **Step 4: Commit**

### Task H2: case A — compact lands on existing PH bg card

- [ ] **Step 1: In feishu.py note_progress("compact_started"), prefer the existing bg card of the most recent detached run with a PH at the fork_point.** If found: update its state to "compacting". If not: case B (new card).

- [ ] **Step 2: On compact_finalized: prepend summary to panel_md, increment compacted_n, restore state to prior (running / completed).**

- [ ] **Step 3: Test scenarios 8 + 9 (spec §7).**

- [ ] **Step 4: Commit**

### Task H3: case B — no PH, create new bg card

- [ ] **Step 1: If no detached run with PH near fork_point, lazy-create a bg card with label = "compact" and no answer panel; state="compacting" → "completed" with summary as the only panel content.**

- [ ] **Step 2: Test scenario 10. Commit.**

---

## Group I — /attach command

**Files:**
- Modify: `src/xana/commands/` (find command router) — add `/attach` command
- Modify: `src/xana/agent_adapter/channels/feishu.py` — render /attach menu with inline buttons
- Test: `tests/test_attach_command.py`

### Task I1: /attach command + result

- [ ] **Step 1: Register `/attach` CommandSpec** with `triggers_turn=False`, `requires_detach=True` (per spec §3.1).
- [ ] **Step 2: Command result lists all running PHs**:

```python
async def run_attach(self, inbound):
    sid = inbound.session_id
    snap = await self.store.snapshot(sid, "chat")
    runs = [m for m in snap if isinstance(m, Placeholder) and m.status == "running"]
    return CommandResult(ok=True, message="选择要 attach 的 run",
        payload={"triggers_turn": False, "requires_detach": True,
                 "attach_choices": [{"run_id": r.run_id, "label": r.label} for r in runs]})
```

- [ ] **Step 3: Test command output + commit**

### Task I2: gateway auto-detach when `requires_detach=True`

- [ ] **Step 1: In gateway `_submit` command handling block, after CommandResult comes back:** if `result.payload.get("requires_detach")`, call `await self.detach_foreground(sid)` before rendering.
- [ ] **Step 2: Test commit**

### Task I3: feishu render /attach menu — inline buttons

- [ ] **Step 1: When event has `attach_choices`** in payload, render a card with one button per choice carrying `{"action":"attach_run","run_id":...}`.
- [ ] **Step 2: Callback flow (already added in E3) routes to `gateway.attach(sid, run_id)`.**
- [ ] **Step 3: Test commit**

---

## Group J — ⏹ Stop / ➡ Background buttons wiring sanity

Mostly done in E3. This group is the end-to-end test of the buttons + ensures cancel propagation reaches the harness.

**Files:**
- Test only: `tests/test_fg_controls_e2e.py` (unit-level, fake sender)

### Task J1: stop button cancels the run

- [ ] **Step 1: Test that clicking ⏹ calls gateway.cancel(sid, fg_run_id); harness emits cancelled event; fg card freezes; bg card (if exists) gets state="cancelled".**
- [ ] **Step 2: Implement any missing wiring**
- [ ] **Step 3: Commit**

### Task J2: ➡ background triggers detach without cancelling

- [ ] **Step 1: Test that clicking ➡ calls detach_foreground; run continues; fg freezes; bg card created; later ph_replaced fires.**
- [ ] **Step 2: Commit**

---

## Group K — Cleanup deprecated code

**Files:**
- `src/xana/agent_adapter/channels/feishu_card.py` — remove `build_completed_bg_card`, `bg_runs` kwarg (done in E1, ensure no callers)
- `src/xana/agent_adapter/channels/feishu.py` — remove old switch_foreground callback action + bg_runs assembly
- `tests/test_feishu_race_e2e.py` — rewrite scenario_c to attach instead of switch_back
- `scripts/race_test/feishu_race.py` — same

### Task K1: dead-code purge

- [ ] **Step 1: Grep for stale identifiers**

```bash
# expected: 0 hits each
Grep "switch_foreground\|bg_runs\|build_completed_bg_card\|foreground_changed" src/ -n
```

- [ ] **Step 2: If hits remain, delete (assert: they are not called by anything kept)**

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x -q
```

- [ ] **Step 4: Commit**

### Task K2: rewrite `test_feishu_race_e2e.py::test_scenario_c_switch_back_mid_flight` → `test_scenario_c_attach_mid_flight`

- [ ] **Step 1: New test body**

```python
@pytest.mark.asyncio
async def test_scenario_c_attach_mid_flight():
    adapter, gateway, fake, sid = await _setup()
    try:
        t1 = await _submit_bg(gateway, adapter, sid, _PROMPT_TOOLS)
        await asyncio.sleep(8)
        t2 = await _submit_bg(gateway, adapter, sid, _PROMPT_HELLO)
        await asyncio.sleep(4)
        # attach back to U1
        bg_id = next(r for r in gateway._inflight[sid] if not r.is_attached).run_id
        await gateway.attach(sid, bg_id)
        await asyncio.sleep(1)
        await asyncio.gather(t1, t2)
    finally:
        await adapter.stop()
    _assert_all_cards_valid(fake)
    _assert_no_frozen_card_has_stop_or_self_switch(fake)
    _assert_no_post_freeze_revert_to_streaming(fake)
    # new assertion: after attach(U1), there's a fresh fg card for U1
    fg_cards_for_r1 = [c for _, c in fake.sends + fake.patches
                       if _is_streaming_card(c)]
    assert len(fg_cards_for_r1) >= 1
```

Update the helper functions (`_assert_no_frozen_card_has_stop_or_self_switch`) to match new "no switch buttons at all" model.

- [ ] **Step 2: Commit**

---

## Group L — E2E race tests (scenarios 1-12 per spec §7)

**Files:**
- Modify: `tests/test_feishu_race_e2e.py` — add scenarios per spec §7 table
- Modify: `scripts/race_test/feishu_race.py` — runner harness for manual debugging

### Task L1: encode scenarios 1-5 + 8-10 + 12 as e2e tests (guarded by RUN_RACE_E2E=1)

- [ ] **Step 1: Add one test per scenario with assertions matching the "期望" column**
- [ ] **Step 2: Run with `RUN_RACE_E2E=1 pytest tests/test_feishu_race_e2e.py -v`** and iterate until green
- [ ] **Step 3: Commit**

### Task L2: scenario 13 (LLM-side PH expansion) — synthetic test

- [ ] **Step 1: Inspect actual LLM request payload via a stub provider that records prompts**
- [ ] **Step 2: Assert the placeholder appears as synthetic user/assistant pair, not absent and not as the real running content**
- [ ] **Step 3: Commit**

### Task L3: scenario 14 (sync mode)

- [ ] **Step 1: Set the session config to sync mode; verify only one fg card exists at a time; second submit's fg waits until first completes**
- [ ] **Step 2: Commit**

### Task L4: scenario 11 (restart with running PH)

- [ ] **Step 1: Crash mid-run (simulate by killing the run task); restart boot; assert PH stays running (we're not doing P5 resume — just verify no crash)**
- [ ] **Step 2: Commit**

---

## Group M — Manual user acceptance

This is a hard gate, not code. After Group L passes:

### Task M1: user manual test on real Feishu

- [ ] **Step 1: Drive xana via real Feishu account; user runs the 15 scenarios from spec §7 by hand**
- [ ] **Step 2: Record any visual regressions; create follow-up tickets if any**
- [ ] **Step 3: User signs off — STOP here, do not proceed without explicit "ok 沉淀"**

### Task M2: user manual test on real Telegram (parallel to M1)

- [ ] **Step 1: Same scenarios on TG**
- [ ] **Step 2: Sign off**

---

## Group N — Sink design to ASYNC_CHAT_DESIGN.md (post-acceptance)

### Task N1: create authoritative doc

- [ ] **Step 1: Copy spec §§1-7 (lightly edited for past-tense status) into `best_agent/ds_review/ROADMAP/ASYNC_CHAT_DESIGN.md`**
- [ ] **Step 2: Add header note: "implemented 2026-06-XX, see `docs/superpowers/plans/2026-06-14-async-chat-implementation.md` for build sequence"**
- [ ] **Step 3: Commit**

### Task N2: cross-reference older docs

- [ ] **Step 1: Edit `best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md` top** to add "渲染与 PH 隔离的修订见 ASYNC_CHAT_DESIGN.md"
- [ ] **Step 2: Update `docs/design/bg-streaming.md` + `docs/design/compact-rendering.md`** to reference new doc (or mark superseded)
- [ ] **Step 3: Mark P6 row in ROADMAP table ✅**
- [ ] **Step 4: Commit**

```bash
git commit -am "docs: sink async-chat design (PH-first + dual-card) to ASYNC_CHAT_DESIGN.md"
```

---

## Risk recap (spec §11)

1. **PromptAssembler change** (Task B4) — new step in a hot path. Add the synthetic-pair test (Task B4) + at least one integration test (Task L2) before considering this stable.
2. **`InflightRun` rename** (Task A1) — wide blast radius. The arch test + a full pytest suite run after A1 are non-negotiable.
3. **Write queue consumer concurrency** (Task C2) — `boot` MUST start the write_consumer as a separate `asyncio.create_task`. The sync-mode scenario test (L3) is the canary; if it deadlocks, the consumer wasn't decoupled.
4. **Card schema validation** — fake-sender e2e (Group L) asserts every send/patch is structurally valid Schema 2.0. Do NOT remove that audit.

---

## Self-review notes

- Spec §1 → Groups A3, B (snapshot/append_placeholder/replace_placeholder/replace_range/PH expansion)
- Spec §2 → Groups E, F, G (renderers)
- Spec §3 → Groups I, J (commands + buttons)
- Spec §4 → Group H (compact)
- Spec §5 → Group A (interfaces)
- Spec §6 → Group K (cleanup)
- Spec §7 → Group L (e2e scenarios)
- Spec §8 → explicitly out of scope (resume/clear)
- Spec §9 → Group N
- Spec §10 task groups A-N → mirrored 1:1 in plan groups

Plan complete and saved to `docs/superpowers/plans/2026-06-14-async-chat-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per Group, review between groups, fast iteration
2. **Inline Execution** — execute groups in this session with checkpoints

Which approach?
