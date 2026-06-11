# xana First Core Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the first end-to-end vertical slice `yuxuc → AgentGateway → ChatAgent → Harness → {Tool, Skill}` with `Memory` injection, strictly conforming to the abstract interfaces in `src/xana/*/interfaces.py`.

**Architecture:** All components implement existing abstract interfaces with no interface edits. Wiring is in-process (no client/server): `runtime.boot.boot()` returns a `DefaultAgentGateway` with all stores/registries pre-wired. CLI (`yuxuc`) registers a `CliAdapter` and pumps stdin/stdout through the gateway. Memory is mounted from `README_ai.md` via existing `MarkdownMemoryTree` + `SQLiteMemorySearchEngine` + `SearchMemoryInjector`.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, pytest, pytest-asyncio (already installed). Stdlib-only at runtime — no extra deps.

---

## Task 0: Git initialization

**Files:**
- Create: `/home/xzp/project/xana/.gitignore`

- [ ] **Step 1: Init repo and set remote**

```bash
cd /home/xzp/project/xana
git init
git remote add origin git@github.com:aicodex/xana.git
git branch -M main
```

Run: `git status` — expected: shows all current files as untracked.

- [ ] **Step 2: Write .gitignore**

```gitignore
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.venv/
.xana/
*.db
.DS_Store
.idea/
.vscode/
build/
dist/
```

- [ ] **Step 3: Initial commit of existing skeleton**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: import xana skeleton (interfaces, contracts, doc_ai, tests)

Brings in the input/output-first agent runtime skeleton: abstract
interfaces for every module, contract dataclasses, the markdown-backed
memory implementation, the canonical fake-pipeline-flow contract test,
and the doc_ai / exp_ai / todo_ai memory tree.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

Run: `git log --oneline` — expected: one commit.

---

## Task 1: pyproject.toml — add console script and packaging

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Read current pyproject**

```bash
cat pyproject.toml
```

- [ ] **Step 2: Rewrite pyproject.toml**

```toml
[project]
name = "xana"
version = "0.1.0"
description = "Input/output-first agent runtime skeleton."
requires-python = ">=3.10"

[project.scripts]
yuxuc = "xana.cli.yuxuc:main"

[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "strict"
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `python -m pytest tests/ -q`
Expected: `72 passed`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "$(cat <<'EOF'
chore: add yuxuc console script and packaging config

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 2: InMemoryActiveContextStore + InMemoryHistoryMirror

**Files:**
- Create: `src/xana/memory/active_context.py`
- Test: `tests/test_in_memory_active_context.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_in_memory_active_context.py`:

```python
from __future__ import annotations

import pytest

from xana.memory.active_context import InMemoryActiveContextStore, InMemoryHistoryMirror
from xana.memory.contracts import HistoryRow, MemoryMessage


@pytest.mark.asyncio
async def test_active_context_append_grows_messages_and_token_estimate() -> None:
    store = InMemoryActiveContextStore()
    ctx1 = await store.append_message("s1", "chat", MemoryMessage(role="user", content="hi"))
    ctx2 = await store.append_message("s1", "chat", MemoryMessage(role="assistant", content="hello there"))
    assert ctx1.messages == (MemoryMessage(role="user", content="hi"),)
    assert len(ctx2.messages) == 2
    assert ctx2.messages[-1].content == "hello there"
    assert ctx2.token_estimate >= ctx1.token_estimate
    assert ctx2.session_id == "s1" and ctx2.agent_name == "chat"


@pytest.mark.asyncio
async def test_active_context_get_returns_none_for_missing_session() -> None:
    store = InMemoryActiveContextStore()
    assert await store.get("missing", "chat") is None


@pytest.mark.asyncio
async def test_active_context_get_returns_appended_context() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="hi"))
    ctx = await store.get("s1", "chat")
    assert ctx is not None
    assert ctx.messages[0].content == "hi"


@pytest.mark.asyncio
async def test_active_context_clear_removes_session() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="hi"))
    await store.clear("s1", "chat")
    assert await store.get("s1", "chat") is None


@pytest.mark.asyncio
async def test_active_context_compact_replaces_summary_and_clears_messages() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="hi"))
    ctx = await store.compact("s1", "chat", "summary text")
    assert ctx.summary == "summary text"
    assert ctx.messages == ()


@pytest.mark.asyncio
async def test_history_mirror_append_and_replay_counts_rows() -> None:
    mirror = InMemoryHistoryMirror()
    await mirror.append(
        HistoryRow(
            row_id="r1",
            session_id="s1",
            agent_name="chat",
            kind="user",
            payload=MemoryMessage(role="user", content="hi"),
            ts=1.0,
        )
    )
    await mirror.append(
        HistoryRow(
            row_id="r2",
            session_id="s1",
            agent_name="chat",
            kind="assistant",
            payload=MemoryMessage(role="assistant", content="ok"),
            ts=2.0,
        )
    )
    result = await mirror.replay()
    assert result.loaded_rows == 2
    assert result.loaded_sessions == 1
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_in_memory_active_context.py -v`
Expected: collection or import error (module does not exist).

- [ ] **Step 3: Implement the module**

Create `src/xana/memory/active_context.py`:

```python
"""In-memory ActiveContextStore + HistoryMirror.

Default runtime authority for active conversation context and durable mirror.
Single-process, non-persistent — suitable for CLI sessions and tests.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from xana.memory.contracts import ActiveContext, HistoryRow, MemoryMessage, ReplayResult
from xana.memory.interfaces import ActiveContextStore, HistoryMirror, MemoryInjector, MemoryInjectorRegistry
from xana.memory.contracts import InjectedBlock


class InMemoryActiveContextStore(ActiveContextStore):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}

    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return self._store.get((session_id, agent_name))

    async def set(self, context: ActiveContext) -> None:
        self._store[(context.session_id, context.agent_name)] = context

    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(
            current,
            messages=current.messages + (message,),
            token_estimate=current.token_estimate + len(message.content),
        )
        self._store[key] = updated
        return updated

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._store.pop((session_id, agent_name), None)


class InMemoryHistoryMirror(HistoryMirror):
    def __init__(self) -> None:
        self._rows: list[HistoryRow] = []

    async def append(self, row: HistoryRow) -> None:
        self._rows.append(row)

    async def replay(self) -> ReplayResult:
        sessions = {row.session_id for row in self._rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(self._rows))

    def rows(self) -> tuple[HistoryRow, ...]:
        return tuple(self._rows)


class DefaultMemoryInjectorRegistry(MemoryInjectorRegistry):
    """Ordered registry of MemoryInjectors. Lower priority runs first."""

    def __init__(self) -> None:
        self._items: list[tuple[int, MemoryInjector]] = []

    async def register(self, injector: MemoryInjector, priority: int = 100) -> None:
        self._items.append((priority, injector))
        self._items.sort(key=lambda x: x[0])

    async def list_active(self) -> tuple[MemoryInjector, ...]:
        return tuple(inj for _, inj in self._items)

    async def collect(
        self,
        session_id: str,
        agent_name: str,
        user_input: str,
        cwd: str,
    ) -> tuple[InjectedBlock, ...]:
        out: list[InjectedBlock] = []
        seen_paths: set[str] = set()
        for _, injector in self._items:
            blocks = await injector.select(session_id, agent_name, user_input, cwd)
            for block in blocks:
                if block.node_path in seen_paths:
                    continue
                seen_paths.add(block.node_path)
                out.append(block)
        return tuple(out)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_in_memory_active_context.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/memory/active_context.py tests/test_in_memory_active_context.py
git commit -m "$(cat <<'EOF'
feat(memory): in-memory ActiveContextStore + HistoryMirror + DefaultMemoryInjectorRegistry

First runtime authority for active conversation context and durable
mirror, plus an ordered injector registry that dedupes by node_path.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 3: DefaultMemoryInjectorRegistry behavior test

**Files:**
- Test: `tests/test_default_memory_injector_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_default_memory_injector_registry.py`:

```python
from __future__ import annotations

import pytest

from xana.memory.active_context import DefaultMemoryInjectorRegistry
from xana.memory.contracts import InjectedBlock
from xana.memory.interfaces import MemoryInjector


class _Stub(MemoryInjector):
    def __init__(self, label: str, blocks: tuple[InjectedBlock, ...]) -> None:
        self.label = label
        self.blocks = blocks

    async def name(self) -> str:
        return self.label

    async def select(self, session_id, agent_name, user_input, cwd):
        return self.blocks


@pytest.mark.asyncio
async def test_collect_returns_blocks_in_priority_order_low_first() -> None:
    reg = DefaultMemoryInjectorRegistry()
    a = _Stub("a", (InjectedBlock(node_path="A", content="a", placement="system_head", reason="t"),))
    b = _Stub("b", (InjectedBlock(node_path="B", content="b", placement="system_head", reason="t"),))
    await reg.register(b, priority=50)
    await reg.register(a, priority=10)
    blocks = await reg.collect("s", "chat", "input", "/cwd")
    assert [bl.node_path for bl in blocks] == ["A", "B"]


@pytest.mark.asyncio
async def test_collect_dedupes_by_node_path_first_wins() -> None:
    reg = DefaultMemoryInjectorRegistry()
    a = _Stub("a", (InjectedBlock(node_path="X", content="first", placement="system_head", reason="t"),))
    b = _Stub("b", (InjectedBlock(node_path="X", content="second", placement="system_head", reason="t"),))
    await reg.register(a, priority=10)
    await reg.register(b, priority=20)
    blocks = await reg.collect("s", "chat", "input", "/cwd")
    assert len(blocks) == 1
    assert blocks[0].content == "first"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_default_memory_injector_registry.py -v`
Expected: 2 passed (implementation already done in Task 2).

- [ ] **Step 3: Commit**

```bash
git add tests/test_default_memory_injector_registry.py
git commit -m "$(cat <<'EOF'
test(memory): behavior tests for DefaultMemoryInjectorRegistry

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 4: InMemoryToolRegistry

**Files:**
- Create: `src/xana/tools/registry.py`
- Test: `tests/test_in_memory_tool_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_in_memory_tool_registry.py`:

```python
from __future__ import annotations

import pytest

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.tools.contracts import ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool
from xana.tools.registry import InMemoryToolRegistry
from xana.policy.contracts import PermissionResult


def _component(name: str) -> ComponentSpec:
    return ComponentSpec(
        kind="tool",
        name=name,
        qualified_name=f"builtin.{name}",
        description=f"{name} tool",
        source=ComponentSource(source_type="builtin", scope="system"),
    )


class _NoopTool(Tool):
    def __init__(self, name: str) -> None:
        self._spec = ToolSpec(component=_component(name), input_schema={})

    def spec(self) -> ToolSpec:
        return self._spec

    async def validate(self, invocation): ...
    async def check_permission(self, invocation, context):
        return PermissionResult(decision="allow")
    async def pre_tool_use(self, invocation, context):
        return invocation
    async def execute(self, invocation, context):
        return ToolResult(ok=True, content=None)
    async def post_tool_use(self, invocation, context, result):
        return result
    async def render_result(self, result):
        return ""
    async def cancel(self, call_id): ...


@pytest.mark.asyncio
async def test_register_and_get_by_component_name() -> None:
    reg = InMemoryToolRegistry()
    tool = _NoopTool("echo")
    await reg.register(tool)
    fetched = await reg.get("echo")
    assert fetched is tool


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    reg = InMemoryToolRegistry()
    assert await reg.get("nope") is None


@pytest.mark.asyncio
async def test_list_specs_returns_all() -> None:
    reg = InMemoryToolRegistry()
    await reg.register(_NoopTool("a"))
    await reg.register(_NoopTool("b"))
    specs = await reg.list_specs()
    assert {s.component.name for s in specs} == {"a", "b"}
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_in_memory_tool_registry.py -v`
Expected: ImportError (registry module does not exist).

- [ ] **Step 3: Implement**

Create `src/xana/tools/registry.py`:

```python
"""In-memory ToolRegistry. Keyed by ComponentSpec.name."""
from __future__ import annotations

from xana.tools.contracts import ToolSpec
from xana.tools.interfaces import Tool, ToolRegistry


class InMemoryToolRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    async def register(self, tool: Tool) -> None:
        name = tool.spec().component.name
        self._tools[name] = tool

    async def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec() for t in self._tools.values())
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_in_memory_tool_registry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/tools/registry.py tests/test_in_memory_tool_registry.py
git commit -m "$(cat <<'EOF'
feat(tools): InMemoryToolRegistry keyed by ComponentSpec.name

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 5: InMemorySkillRegistry

**Files:**
- Create: `src/xana/skills/registry.py`
- Test: `tests/test_in_memory_skill_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_in_memory_skill_registry.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import SkillActivation, SkillContribution, SkillManifest, SkillPackage, SkillSpec
from xana.skills.interfaces import Skill
from xana.skills.registry import InMemorySkillRegistry


def _spec(name: str) -> SkillSpec:
    return SkillSpec(
        component=ComponentSpec(
            kind="skill",
            name=name,
            qualified_name=f"builtin.{name}",
            description=f"{name} skill",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        when_to_use="always",
        package=SkillPackage(
            root=None,
            manifest=SkillManifest(name=name, description=f"{name} skill"),
            instructions_path=None,
        ),
    )


class _StubSkill(Skill):
    def __init__(self, name: str) -> None:
        self._spec = _spec(name)
        self.activations: list[SkillActivation] = []

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        self.activations.append(activation)
        return SkillContribution(prompt_blocks=(f"[{self._spec.component.name}] block",))

    async def deactivate(self, session_id: str) -> None: ...


@pytest.mark.asyncio
async def test_register_get_and_discover() -> None:
    reg = InMemorySkillRegistry()
    a = _StubSkill("identity")
    b = _StubSkill("hello")
    await reg.register(a)
    await reg.register(b)
    assert await reg.get("identity") is a
    discovered = await reg.discover(
        SkillActivation(kind="model", session_id="s", agent_name="chat")
    )
    assert set(discovered) == {a, b}


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    reg = InMemorySkillRegistry()
    assert await reg.get("nope") is None
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_in_memory_skill_registry.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/skills/registry.py`:

```python
"""In-memory SkillRegistry.

discover() returns all registered skills for v1. Future versions can filter
by activation_paths against cwd / kind / agent.
"""
from __future__ import annotations

from xana.skills.contracts import SkillActivation
from xana.skills.interfaces import Skill, SkillRegistry


class InMemorySkillRegistry(SkillRegistry):
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    async def register(self, skill: Skill) -> None:
        name = skill.spec().component.name
        self._skills[name] = skill

    async def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    async def discover(self, activation: SkillActivation) -> tuple[Skill, ...]:
        return tuple(self._skills.values())
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_in_memory_skill_registry.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/skills/registry.py tests/test_in_memory_skill_registry.py
git commit -m "$(cat <<'EOF'
feat(skills): InMemorySkillRegistry with discover() returning all skills

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 6: InMemoryStateRepository (SessionStore + RunStore)

**Files:**
- Create: `src/xana/state/in_memory.py`
- Test: `tests/test_in_memory_state.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_in_memory_state.py`:

```python
from __future__ import annotations

import pytest

from xana.state.contracts import Checkpoint, RunRecord, SessionRecord, UsageSnapshot
from xana.state.in_memory import InMemoryStateRepository


@pytest.mark.asyncio
async def test_session_upsert_and_get() -> None:
    repo = InMemoryStateRepository()
    sessions = repo.sessions()
    await sessions.upsert(SessionRecord(session_id="s1", current_agent="chat"))
    rec = await sessions.get("s1")
    assert rec is not None and rec.current_agent == "chat"


@pytest.mark.asyncio
async def test_run_create_and_update_status() -> None:
    repo = InMemoryStateRepository()
    runs = repo.runs()
    run = RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running")
    await runs.create(run)
    updated = await runs.update_status("r1", "completed")
    assert updated.status == "completed"


@pytest.mark.asyncio
async def test_run_update_usage_persists() -> None:
    repo = InMemoryStateRepository()
    runs = repo.runs()
    await runs.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    snap = UsageSnapshot(estimated_input_tokens=10, exact_input_tokens=8)
    await runs.update_usage("r1", snap)
    fetched = await runs.get("r1")
    assert fetched is not None
    assert fetched.usage.exact_input_tokens == 8


@pytest.mark.asyncio
async def test_list_unfinished_returns_only_running_or_paused() -> None:
    repo = InMemoryStateRepository()
    runs = repo.runs()
    await runs.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    await runs.create(RunRecord(run_id="r2", session_id="s1", agent_name="chat", status="completed"))
    unfinished = await runs.list_unfinished()
    assert {r.run_id for r in unfinished} == {"r1"}


@pytest.mark.asyncio
async def test_checkpoint_records_last_checkpoint_on_run() -> None:
    repo = InMemoryStateRepository()
    runs = repo.runs()
    await runs.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    await runs.checkpoint("r1", Checkpoint(name="turn_started", run_id="r1", ts=1.0))
    fetched = await runs.get("r1")
    assert fetched is not None
    assert fetched.last_checkpoint is not None
    assert fetched.last_checkpoint.name == "turn_started"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_in_memory_state.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/state/in_memory.py`:

```python
"""In-memory state repository for sessions and runs."""
from __future__ import annotations

from dataclasses import replace

from xana.state.contracts import Checkpoint, RunRecord, RunStatus, SessionRecord, UsageSnapshot
from xana.state.interfaces import RunStore, SessionStore, StateRepository


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._store: dict[str, SessionRecord] = {}

    async def get(self, session_id: str) -> SessionRecord | None:
        return self._store.get(session_id)

    async def upsert(self, record: SessionRecord) -> None:
        self._store[record.session_id] = record

    async def patch(self, session_id: str, fields: dict) -> SessionRecord:
        current = self._store[session_id]
        updated = replace(current, **fields)
        self._store[session_id] = updated
        return updated

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self._store.values())

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class InMemoryRunStore(RunStore):
    def __init__(self) -> None:
        self._store: dict[str, RunRecord] = {}

    async def create(self, record: RunRecord) -> None:
        self._store[record.run_id] = record

    async def get(self, run_id: str) -> RunRecord | None:
        return self._store.get(run_id)

    async def update_status(self, run_id: str, status: RunStatus, fields: dict | None = None) -> RunRecord:
        current = self._store[run_id]
        extras = fields or {}
        updated = replace(current, status=status, **extras)
        self._store[run_id] = updated
        return updated

    async def checkpoint(self, run_id: str, checkpoint: Checkpoint) -> None:
        current = self._store[run_id]
        self._store[run_id] = replace(current, last_checkpoint=checkpoint)

    async def update_usage(self, run_id: str, usage: UsageSnapshot) -> None:
        current = self._store[run_id]
        self._store[run_id] = replace(current, usage=usage)

    async def list_unfinished(self) -> tuple[RunRecord, ...]:
        return tuple(r for r in self._store.values() if r.status in ("running", "paused"))

    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]:
        return tuple(r for r in self._store.values() if r.session_id == session_id)


class InMemoryStateRepository(StateRepository):
    def __init__(self) -> None:
        self._sessions = InMemorySessionStore()
        self._runs = InMemoryRunStore()

    def sessions(self) -> SessionStore:
        return self._sessions

    def runs(self) -> RunStore:
        return self._runs
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_in_memory_state.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/state/in_memory.py tests/test_in_memory_state.py
git commit -m "$(cat <<'EOF'
feat(state): InMemoryStateRepository with session and run stores

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 7: FakeModelClient + InMemoryModelRegistry

**Files:**
- Create: `src/xana/llm_driver/fake_client.py`
- Test: `tests/test_fake_model_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fake_model_client.py`:

```python
from __future__ import annotations

import pytest

from xana.llm_driver.contracts import ModelEvent, ModelRequest
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn


@pytest.mark.asyncio
async def test_stream_yields_scripted_events_in_order() -> None:
    client = FakeModelClient(
        ScriptedTurn(
            text_deltas=("hel", "lo"),
            tool_call=None,
            usage_input_tokens=3,
            usage_output_tokens=5,
        )
    )
    req = ModelRequest(provider="fake", model="m", messages=())
    kinds = [event.kind async for event in client.stream(req)]
    assert kinds == ["text_delta", "text_delta", "usage", "done"]


@pytest.mark.asyncio
async def test_stream_emits_tool_call_when_scripted() -> None:
    client = FakeModelClient(
        ScriptedTurn(
            text_deltas=(),
            tool_call=("memory_search", {"query": "auth"}, "call-1"),
            usage_input_tokens=1,
            usage_output_tokens=1,
        )
    )
    req = ModelRequest(provider="fake", model="m", messages=())
    events = [event async for event in client.stream(req)]
    tool_events = [e for e in events if e.kind == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].payload["tool_name"] == "memory_search"
    assert tool_events[0].payload["call_id"] == "call-1"
    assert tool_events[0].payload["input"] == {"query": "auth"}


@pytest.mark.asyncio
async def test_capabilities_reports_fake_provider() -> None:
    client = FakeModelClient(ScriptedTurn())
    caps = await client.capabilities()
    assert caps.provider == "fake"


@pytest.mark.asyncio
async def test_registry_register_and_get() -> None:
    reg = InMemoryModelRegistry()
    client = FakeModelClient(ScriptedTurn())
    await reg.register("fake", client)
    assert await reg.get("fake") is client
    assert await reg.get("missing") is None
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_fake_model_client.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/llm_driver/fake_client.py`:

```python
"""Fake LLM client for tests and offline development.

Scripts a sequence of ModelEvents: text deltas, optional tool call, usage, done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from xana.llm_driver.contracts import ModelEvent, ModelRequest, ModelUsage, ProviderCapabilities
from xana.llm_driver.interfaces import ModelClient, ModelRegistry


@dataclass(frozen=True)
class ScriptedTurn:
    text_deltas: tuple[str, ...] = ()
    tool_call: tuple[str, dict[str, Any], str] | None = None  # (tool_name, input, call_id)
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


class FakeModelClient(ModelClient):
    def __init__(self, *turns: ScriptedTurn) -> None:
        self._turns: list[ScriptedTurn] = list(turns) or [ScriptedTurn()]
        self._cursor = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        turn = self._turns[min(self._cursor, len(self._turns) - 1)]
        self._cursor += 1
        for delta in turn.text_deltas:
            yield ModelEvent(kind="text_delta", payload={"text": delta})
        if turn.tool_call is not None:
            name, input_, call_id = turn.tool_call
            yield ModelEvent(
                kind="tool_call",
                payload={"tool_name": name, "input": input_, "call_id": call_id},
            )
        yield ModelEvent(
            kind="usage",
            usage=ModelUsage(
                input_tokens=turn.usage_input_tokens,
                output_tokens=turn.usage_output_tokens,
            ),
        )
        yield ModelEvent(kind="done")

    async def cancel(self, request_id: str) -> None:
        return None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="fake", supports_streaming=True, supports_tools=True)


class InMemoryModelRegistry(ModelRegistry):
    def __init__(self) -> None:
        self._clients: dict[str, ModelClient] = {}

    async def register(self, provider: str, client: ModelClient) -> None:
        self._clients[provider] = client

    async def get(self, provider: str) -> ModelClient | None:
        return self._clients.get(provider)
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_fake_model_client.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/llm_driver/fake_client.py tests/test_fake_model_client.py
git commit -m "$(cat <<'EOF'
feat(llm_driver): FakeModelClient + InMemoryModelRegistry

Scripted ScriptedTurn drives text-delta + optional tool-call + usage +
done events. No network. Enables harness/E2E tests offline.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 8: MemorySearchTool (first built-in Tool)

**Files:**
- Create: `src/xana/tools/builtin/__init__.py`
- Create: `src/xana/tools/builtin/memory_search.py`
- Test: `tests/test_memory_search_tool.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_memory_search_tool.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.memory.contracts import MemoryNode, SearchHit, SearchQuery
from xana.memory.interfaces import MemorySearchEngine
from xana.policy.contracts import PermissionContext
from xana.tools.builtin.memory_search import MemorySearchTool
from xana.tools.contracts import ToolContext, ToolInvocation


class _StubEngine(MemorySearchEngine):
    def __init__(self) -> None:
        self.last_query: SearchQuery | None = None

    async def index(self, node: MemoryNode) -> None: ...
    async def remove(self, node_path: str) -> None: ...

    async def search(self, query: SearchQuery):
        self.last_query = query
        node = MemoryNode(path="doc_ai/memory.md", kind="doc", title="memory", body="hit body")
        return (SearchHit(node_path="doc_ai/memory.md", score=0.9, matched_spans=("body",), node=node),)


def _context() -> ToolContext:
    return ToolContext(
        session_id="s",
        agent_name="chat",
        cwd="/tmp",
        permission=PermissionContext(mode="default", session_id="s"),
    )


@pytest.mark.asyncio
async def test_execute_returns_search_hits_in_content() -> None:
    engine = _StubEngine()
    tool = MemorySearchTool(engine)
    invocation = ToolInvocation(
        tool_name="memory_search",
        input={"query": "memory"},
        call_id="c1",
        session_id="s",
    )
    result = await tool.execute(invocation, _context())
    assert result.ok is True
    assert "doc_ai/memory.md" in result.rendered
    assert engine.last_query is not None
    assert engine.last_query.text == "memory"


@pytest.mark.asyncio
async def test_check_permission_always_allow() -> None:
    tool = MemorySearchTool(_StubEngine())
    invocation = ToolInvocation(
        tool_name="memory_search", input={"query": "x"}, call_id="c1", session_id="s"
    )
    result = await tool.check_permission(invocation, _context())
    assert result.decision == "allow"


@pytest.mark.asyncio
async def test_validate_rejects_missing_query() -> None:
    tool = MemorySearchTool(_StubEngine())
    invocation = ToolInvocation(tool_name="memory_search", input={}, call_id="c1", session_id="s")
    with pytest.raises(ValueError):
        await tool.validate(invocation)


@pytest.mark.asyncio
async def test_spec_name_is_memory_search() -> None:
    tool = MemorySearchTool(_StubEngine())
    assert tool.spec().component.name == "memory_search"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_memory_search_tool.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/tools/builtin/__init__.py`:

```python
"""Built-in tools."""
```

Create `src/xana/tools/builtin/memory_search.py`:

```python
"""memory_search: model-side L2 query against the shared MemorySearchEngine."""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.memory.contracts import SearchQuery
from xana.memory.interfaces import MemorySearchEngine
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="memory_search",
        qualified_name="builtin.memory_search",
        description="Search the project memory tree.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
    read_only=True,
)


class MemorySearchTool(Tool):
    def __init__(self, engine: MemorySearchEngine) -> None:
        self.engine = engine

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        query = invocation.input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("memory_search requires non-empty 'query'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="read-only memory query")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        query_text = str(invocation.input.get("query", ""))
        top_k = int(invocation.input.get("top_k", 5))
        hits = await self.engine.search(SearchQuery(text=query_text, top_k=top_k))
        lines = [f"- {hit.node_path} (score={hit.score:.2f})" for hit in hits]
        rendered = "\n".join(lines) if lines else "(no hits)"
        return ToolResult(
            ok=True,
            content=[{"node_path": h.node_path, "score": h.score, "matched_spans": list(h.matched_spans)} for h in hits],
            rendered=rendered,
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_memory_search_tool.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/tools/builtin/__init__.py src/xana/tools/builtin/memory_search.py tests/test_memory_search_tool.py
git commit -m "$(cat <<'EOF'
feat(tools): memory_search built-in tool

Wraps the shared MemorySearchEngine for model-side L2 queries. Same
engine instance feeds the L1 SearchMemoryInjector at prompt time.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 9: IdentitySkill (first built-in Skill)

**Files:**
- Create: `src/xana/skills/builtin/__init__.py`
- Create: `src/xana/skills/builtin/identity.py`
- Test: `tests/test_identity_skill.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_identity_skill.py`:

```python
from __future__ import annotations

import pytest

from xana.skills.builtin.identity import IdentitySkill
from xana.skills.contracts import SkillActivation


@pytest.mark.asyncio
async def test_activate_returns_identity_prompt_block() -> None:
    skill = IdentitySkill()
    contribution = await skill.activate(
        SkillActivation(kind="model", session_id="s", agent_name="chat")
    )
    assert len(contribution.prompt_blocks) >= 1
    assert "xana" in contribution.prompt_blocks[0].lower()


@pytest.mark.asyncio
async def test_spec_name_is_identity() -> None:
    skill = IdentitySkill()
    assert skill.spec().component.name == "identity"


@pytest.mark.asyncio
async def test_activate_accepts_custom_identity_text() -> None:
    skill = IdentitySkill(identity_text="You are xana chat. Reply briefly.")
    contribution = await skill.activate(
        SkillActivation(kind="model", session_id="s", agent_name="chat")
    )
    assert "Reply briefly" in contribution.prompt_blocks[0]
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_identity_skill.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/skills/builtin/__init__.py`:

```python
"""Built-in skills."""
```

Create `src/xana/skills/builtin/identity.py`:

```python
"""identity: minimal always-on skill that injects an identity prompt block."""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import (
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillPackage,
    SkillSpec,
)
from xana.skills.interfaces import Skill


_DEFAULT_IDENTITY = (
    "You are xana, a clean refactor of the yuxu agent runtime. "
    "Answer concisely and use tools when they help."
)


class IdentitySkill(Skill):
    def __init__(self, identity_text: str = _DEFAULT_IDENTITY) -> None:
        self.identity_text = identity_text
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="identity",
                qualified_name="builtin.identity",
                description="Always-on identity prompt for the chat agent.",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="every turn",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="identity", description="agent identity"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(self.identity_text,))

    async def deactivate(self, session_id: str) -> None:
        return None
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_identity_skill.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/skills/builtin/__init__.py src/xana/skills/builtin/identity.py tests/test_identity_skill.py
git commit -m "$(cat <<'EOF'
feat(skills): IdentitySkill — always-on identity prompt block

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 10: DefaultHarness — orchestration loop

**Files:**
- Create: `src/xana/harness_sdk/default.py`
- Test: `tests/test_default_harness.py`

This is the biggest single task. The harness owns the entire turn lifecycle: memory append, injector collect, skill discovery, prompt assembly, model stream consumption, tool dispatch, event emission.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_default_harness.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.memory.contracts import InjectedBlock
from xana.memory.interfaces import MemoryInjector
from xana.policy.contracts import PermissionContext, PermissionResult
from xana.skills.builtin.identity import IdentitySkill
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.tools.contracts import ToolResult, ToolSpec
from xana.tools.interfaces import Tool
from xana.tools.registry import InMemoryToolRegistry


class _EchoTool(Tool):
    def __init__(self) -> None:
        self._spec = ToolSpec(
            component=ComponentSpec(
                kind="tool",
                name="echo",
                qualified_name="builtin.echo",
                description="echo",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            input_schema={"type": "object"},
        )

    def spec(self) -> ToolSpec:
        return self._spec

    async def validate(self, invocation): ...
    async def check_permission(self, invocation, context):
        return PermissionResult(decision="allow")
    async def pre_tool_use(self, invocation, context):
        return invocation
    async def execute(self, invocation, context):
        value = invocation.input.get("value", "")
        return ToolResult(ok=True, content=value, rendered=f"echo:{value}")
    async def post_tool_use(self, invocation, context, result):
        return result
    async def render_result(self, result):
        return result.rendered or ""
    async def cancel(self, call_id): ...


class _StaticInjector(MemoryInjector):
    async def name(self):
        return "static"
    async def select(self, session_id, agent_name, user_input, cwd):
        return (
            InjectedBlock(
                node_path="README_ai.md",
                content="MEM-BLOCK",
                placement="system_head",
                reason="always_on",
            ),
        )


async def _build_context(*, run_id: str = "r1") -> tuple[HarnessRequest, HarnessContext, InMemoryActiveContextStore, InMemoryHistoryMirror, InMemoryToolRegistry]:
    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
    tools = InMemoryToolRegistry()
    await tools.register(_EchoTool())
    skills = InMemorySkillRegistry()
    await skills.register(IdentitySkill(identity_text="IDENTITY"))
    model_registry = InMemoryModelRegistry()
    await model_registry.register("fake", FakeModelClient(
        ScriptedTurn(
            text_deltas=("hel", "lo"),
            tool_call=("echo", {"value": "pong"}, "call-1"),
            usage_input_tokens=3,
            usage_output_tokens=5,
        )
    ))
    injector_registry = DefaultMemoryInjectorRegistry()
    await injector_registry.register(_StaticInjector(), priority=10)
    state = InMemoryStateRepository()

    request = HarnessRequest(session_id="s1", agent_name="chat", user_input="hello world")
    context = HarnessContext(
        session_id="s1",
        agent_name="chat",
        cwd="/tmp",
        model="fake-model",
        run_id=run_id,
        permission=PermissionContext(mode="default", session_id="s1"),
        state_repository=state,
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        config_snapshot={"provider": "fake"},
    )
    return request, context, active_ctx, mirror, tools


@pytest.mark.asyncio
async def test_invoke_yields_tool_finished_then_final_in_order() -> None:
    request, context, _, _, _ = await _build_context()
    harness = DefaultHarness()
    events = [event async for event in harness.invoke(request, context)]
    kinds = [e.kind for e in events]
    assert kinds[-2:] == ["tool_finished", "final"]


@pytest.mark.asyncio
async def test_invoke_appends_user_then_assistant_to_active_context() -> None:
    request, context, active_ctx, _, _ = await _build_context()
    harness = DefaultHarness()
    [_ async for _ in harness.invoke(request, context)]
    snapshot = await active_ctx.get("s1", "chat")
    assert snapshot is not None
    roles = [m.role for m in snapshot.messages]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"


@pytest.mark.asyncio
async def test_invoke_mirrors_user_row_to_history() -> None:
    request, context, _, mirror, _ = await _build_context()
    harness = DefaultHarness()
    [_ async for _ in harness.invoke(request, context)]
    rows = mirror.rows()
    user_rows = [r for r in rows if r.kind == "user"]
    assert len(user_rows) == 1
    assert user_rows[0].payload.content == "hello world"


@pytest.mark.asyncio
async def test_invoke_collects_memory_injection_into_system_prompt() -> None:
    """The injector block must appear inside the assembled prompt the model saw."""
    request, context, _, _, _ = await _build_context()
    harness = DefaultHarness()
    [_ async for _ in harness.invoke(request, context)]
    assembled = harness.last_assembled_messages
    assert assembled is not None
    system_msg = next(m for m in assembled if m.role == "system")
    assert "MEM-BLOCK" in system_msg.content
    assert "IDENTITY" in system_msg.content


@pytest.mark.asyncio
async def test_invoke_emits_final_event_with_usage_snapshot() -> None:
    request, context, _, _, _ = await _build_context()
    harness = DefaultHarness()
    events = [event async for event in harness.invoke(request, context)]
    final = events[-1]
    assert final.kind == "final"
    assert final.usage is not None
    assert final.usage.exact_input_tokens == 3
    assert final.usage.exact_output_tokens == 5


@pytest.mark.asyncio
async def test_invoke_tool_finished_payload_carries_rendered_text() -> None:
    request, context, _, _, _ = await _build_context()
    harness = DefaultHarness()
    events = [event async for event in harness.invoke(request, context)]
    tf = next(e for e in events if e.kind == "tool_finished")
    assert "echo:pong" in tf.payload.get("text", "")


@pytest.mark.asyncio
async def test_invoke_emits_error_when_tool_missing_from_registry() -> None:
    request, context, _, _, tools = await _build_context()
    # Replace registry with empty
    context = HarnessContext(  # rebuild with empty tools
        session_id=context.session_id,
        agent_name=context.agent_name,
        cwd=context.cwd,
        model=context.model,
        run_id=context.run_id,
        permission=context.permission,
        state_repository=context.state_repository,
        active_context_store=context.active_context_store,
        history_mirror=context.history_mirror,
        memory_injector_registry=context.memory_injector_registry,
        tool_registry=InMemoryToolRegistry(),
        skill_registry=context.skill_registry,
        model_registry=context.model_registry,
        config_snapshot=context.config_snapshot,
    )
    harness = DefaultHarness()
    events = [event async for event in harness.invoke(request, context)]
    error_events = [e for e in events if e.kind == "error"]
    assert error_events, "expected an error event when tool is not registered"
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `python -m pytest tests/test_default_harness.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement DefaultHarness**

Create `src/xana/harness_sdk/default.py`:

```python
"""DefaultHarness — first concrete turn orchestrator.

Responsibilities (per design spec doc 2026-06-11):
  1. append user message to ActiveContextStore + HistoryMirror
  2. collect prompt blocks via MemoryInjectorRegistry
  3. discover + activate skills via SkillRegistry
  4. assemble ModelMessages (system header + history)
  5. stream the model (provider taken from context.config_snapshot["provider"])
  6. dispatch tool calls through the full Tool lifecycle
  7. accumulate usage
  8. emit ordered HarnessEvents: optional model_delta, tool_finished per call,
     final at the end. error on failure.
  9. append assistant message + mirror it

State authority is preserved: ActiveContextStore for context, HistoryMirror
for durable log, RunStore (via StateRepository) for run metadata.
"""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncIterator

from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest
from xana.harness_sdk.interfaces import Harness
from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.skills.contracts import SkillActivation
from xana.state.contracts import RunRecord, UsageSnapshot
from xana.tools.contracts import ToolContext, ToolInvocation


class DefaultHarness(Harness):
    def __init__(self) -> None:
        self.last_assembled_messages: tuple[ModelMessage, ...] | None = None

    async def invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]:
        async for event in self._invoke(request, context):
            yield event

    async def _invoke(self, request: HarnessRequest, context: HarnessContext) -> AsyncIterator[HarnessEvent]:
        run_id = context.run_id or "run-anon"
        session_id = request.session_id
        agent_name = request.agent_name

        if context.state_repository is not None:
            await context.state_repository.runs().create(
                RunRecord(run_id=run_id, session_id=session_id, agent_name=agent_name, status="running")
            )

        assert context.active_context_store is not None, "ActiveContextStore required"
        assert context.history_mirror is not None, "HistoryMirror required"

        user_msg = MemoryMessage(role="user", content=request.user_input)
        await context.active_context_store.append_message(session_id, agent_name, user_msg)
        await context.history_mirror.append(
            HistoryRow(
                row_id=f"{run_id}:user",
                session_id=session_id,
                agent_name=agent_name,
                kind="user",
                payload=user_msg,
                ts=0.0,
            )
        )

        injected_head: list[str] = []
        injected_tail: list[str] = []
        if context.memory_injector_registry is not None:
            blocks = await context.memory_injector_registry.collect(
                session_id, agent_name, request.user_input, context.cwd
            )
            for block in blocks:
                target = injected_tail if block.placement == "system_tail" else injected_head
                target.append(block.content)

        skill_blocks: list[str] = []
        if context.skill_registry is not None:
            skills = await context.skill_registry.discover(
                SkillActivation(kind="model", session_id=session_id, agent_name=agent_name, run_id=run_id)
            )
            for skill in skills:
                contribution = await skill.activate(
                    SkillActivation(kind="model", session_id=session_id, agent_name=agent_name, run_id=run_id)
                )
                skill_blocks.extend(contribution.prompt_blocks)

        system_parts = injected_head + skill_blocks + injected_tail
        system_content = "\n\n".join(p for p in system_parts if p)

        snapshot = await context.active_context_store.get(session_id, agent_name)
        history_messages = snapshot.messages if snapshot is not None else (user_msg,)
        history_model_messages = tuple(
            ModelMessage(
                role="user" if m.role == "user" else ("assistant" if m.role == "assistant" else "system"),
                content=m.content,
            )
            for m in history_messages
        )
        assembled = (ModelMessage(role="system", content=system_content),) + history_model_messages
        self.last_assembled_messages = assembled

        provider = str(context.config_snapshot.get("provider", "fake"))
        if context.model_registry is None:
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": "model_registry missing"},
            )
            return
        client = await context.model_registry.get(provider)
        if client is None:
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": f"provider {provider!r} not registered"},
            )
            return

        model_request = ModelRequest(provider=provider, model=context.model, messages=assembled)
        usage = UsageSnapshot(model=context.model, provider=provider)
        text_accumulator: list[str] = []
        tool_outputs: list[str] = []

        try:
            async for event in client.stream(model_request):
                if event.kind == "text_delta":
                    delta = event.payload.get("text", "")
                    text_accumulator.append(delta)
                    yield HarnessEvent(
                        kind="model_delta",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"text": delta},
                    )
                elif event.kind == "tool_call":
                    tool_name = event.payload.get("tool_name", "")
                    call_id = event.payload.get("call_id", "call-anon")
                    tool_input = event.payload.get("input", {}) or {}
                    if context.tool_registry is None:
                        yield HarnessEvent(
                            kind="error",
                            session_id=session_id,
                            run_id=run_id,
                            payload={"error": "tool_registry missing"},
                        )
                        return
                    tool = await context.tool_registry.get(tool_name)
                    if tool is None:
                        yield HarnessEvent(
                            kind="error",
                            session_id=session_id,
                            run_id=run_id,
                            payload={"error": f"tool {tool_name!r} not registered"},
                        )
                        return
                    invocation = ToolInvocation(
                        tool_name=tool_name,
                        input=tool_input,
                        call_id=call_id,
                        session_id=session_id,
                        run_id=run_id,
                    )
                    tool_ctx = ToolContext(
                        session_id=session_id,
                        agent_name=agent_name,
                        cwd=context.cwd,
                        permission=context.permission or _default_permission(session_id),
                        run_id=run_id,
                    )
                    await tool.validate(invocation)
                    perm = await tool.check_permission(invocation, tool_ctx)
                    if perm.decision != "allow":
                        result = await _denied_result(tool, perm)
                    else:
                        prepared = await tool.pre_tool_use(invocation, tool_ctx)
                        result = await tool.execute(prepared, tool_ctx)
                        result = await tool.post_tool_use(prepared, tool_ctx, result)
                    rendered = await tool.render_result(result)
                    tool_outputs.append(rendered)
                    yield HarnessEvent(
                        kind="tool_finished",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"text": rendered, "tool_name": tool_name, "ok": result.ok},
                    )
                elif event.kind == "usage" and event.usage is not None:
                    usage = replace(
                        usage,
                        exact_input_tokens=event.usage.input_tokens,
                        exact_output_tokens=event.usage.output_tokens,
                    )
                elif event.kind == "done":
                    break
                elif event.kind == "error" and event.error is not None:
                    yield HarnessEvent(
                        kind="error",
                        session_id=session_id,
                        run_id=run_id,
                        payload={"error": event.error.message},
                    )
                    return
        except Exception as exc:  # provider exception
            yield HarnessEvent(
                kind="error",
                session_id=session_id,
                run_id=run_id,
                payload={"error": str(exc)},
            )
            return

        final_text = "".join(text_accumulator)
        if tool_outputs:
            tool_section = "\n".join(tool_outputs)
            final_text = f"{final_text}\n{tool_section}" if final_text else tool_section
        assistant_msg = MemoryMessage(role="assistant", content=final_text)
        await context.active_context_store.append_message(session_id, agent_name, assistant_msg)
        await context.history_mirror.append(
            HistoryRow(
                row_id=f"{run_id}:assistant",
                session_id=session_id,
                agent_name=agent_name,
                kind="assistant",
                payload=assistant_msg,
                ts=0.0,
            )
        )
        if context.state_repository is not None:
            await context.state_repository.runs().update_usage(run_id, usage)
            await context.state_repository.runs().update_status(run_id, "completed")

        yield HarnessEvent(
            kind="final",
            session_id=session_id,
            run_id=run_id,
            payload={"text": final_text},
            usage=usage,
        )


def _default_permission(session_id: str):
    from xana.policy.contracts import PermissionContext

    return PermissionContext(mode="default", session_id=session_id)


async def _denied_result(tool, perm):
    from xana.tools.contracts import ToolResult

    return ToolResult(
        ok=False,
        content=None,
        error=perm.reason or "permission denied",
        failure_kind="policy_forbidden",
        rendered=f"[denied: {perm.reason or 'no reason'}]",
    )
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_default_harness.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run all tests to confirm no regression**

Run: `python -m pytest tests/ -q`
Expected: previous count + new tests, all passing.

- [ ] **Step 6: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/test_default_harness.py
git commit -m "$(cat <<'EOF'
feat(harness_sdk): DefaultHarness — turn orchestration loop

Owns the full turn lifecycle: append user msg to ActiveContext/History,
collect MemoryInjector blocks, discover+activate skills, assemble
ModelMessages (system header + history), stream the model, dispatch
tool calls through Tool lifecycle, accumulate usage, emit ordered
HarnessEvents (model_delta / tool_finished / final / error), append
assistant msg + mirror it. State authority preserved: ActiveContext
owns runtime context, HistoryMirror is a mirror, RunStore tracks runs.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 11: ChatAgent + mappers

**Files:**
- Create: `src/xana/agents/chat_agent.py`
- Test: `tests/test_chat_agent.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_agent.py`:

```python
from __future__ import annotations

import pytest

from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.contracts import AgentRequest
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.skills.builtin.identity import IdentitySkill
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.tools.registry import InMemoryToolRegistry


async def _build_agent() -> ChatAgent:
    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
    tools = InMemoryToolRegistry()
    skills = InMemorySkillRegistry()
    await skills.register(IdentitySkill())
    model_registry = InMemoryModelRegistry()
    await model_registry.register(
        "fake",
        FakeModelClient(ScriptedTurn(text_deltas=("hi",), usage_input_tokens=1, usage_output_tokens=2)),
    )
    return ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(),
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=DefaultMemoryInjectorRegistry(),
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=InMemoryStateRepository(),
        cwd="/tmp",
        model="fake-model",
        provider="fake",
    )


@pytest.mark.asyncio
async def test_send_yields_at_least_one_event() -> None:
    agent = await _build_agent()
    request = AgentRequest(session_id="s1", text="hello", source="cli")
    events = [event async for event in agent.send(request)]
    assert events, "agent.send must yield at least one event"


@pytest.mark.asyncio
async def test_send_final_event_kind_is_final() -> None:
    agent = await _build_agent()
    request = AgentRequest(session_id="s1", text="hello", source="cli")
    events = [event async for event in agent.send(request)]
    assert events[-1].kind == "final"


@pytest.mark.asyncio
async def test_send_intermediate_events_are_draft_kind() -> None:
    agent = await _build_agent()
    request = AgentRequest(session_id="s1", text="hello", source="cli")
    events = [event async for event in agent.send(request)]
    for event in events[:-1]:
        assert event.kind == "draft"


@pytest.mark.asyncio
async def test_spec_component_name_is_chat() -> None:
    agent = await _build_agent()
    assert agent.spec().component.name == "chat"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_chat_agent.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/agents/chat_agent.py`:

```python
"""ChatAgent: default agent that calls DefaultHarness with mapped request/context."""
from __future__ import annotations

from typing import AsyncIterator

from xana.agents.contracts import (
    AgentControlRequest,
    AgentEvent,
    AgentRequest,
    AgentSpec,
)
from xana.agents.interfaces import (
    Agent,
    AgentEventMapper,
    AgentRequestMapper,
)
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.contracts import HarnessContext, HarnessEvent, HarnessRequest
from xana.harness_sdk.interfaces import Harness
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
)
from xana.policy.contracts import PermissionContext
from xana.skills.interfaces import SkillRegistry
from xana.state.interfaces import StateRepository
from xana.tools.interfaces import ToolRegistry


def build_chat_agent_spec() -> AgentSpec:
    return AgentSpec(
        component=ComponentSpec(
            kind="agent",
            name="chat",
            qualified_name="builtin.chat",
            description="Default chat agent.",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        prompt_id="chat-default",
    )


class DefaultAgentRequestMapper(AgentRequestMapper):
    def __init__(
        self,
        *,
        active_context_store: ActiveContextStore,
        history_mirror: HistoryMirror,
        memory_injector_registry: MemoryInjectorRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
    ) -> None:
        self.active_context_store = active_context_store
        self.history_mirror = history_mirror
        self.memory_injector_registry = memory_injector_registry
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.model_registry = model_registry
        self.state_repository = state_repository
        self.cwd = cwd
        self.model = model
        self.provider = provider
        self._run_counter = 0

    async def to_harness_request(self, spec: AgentSpec, request: AgentRequest) -> HarnessRequest:
        return HarnessRequest(
            session_id=request.session_id,
            agent_name=spec.component.name,
            user_input=request.text,
            metadata=dict(request.metadata),
        )

    async def to_harness_context(self, spec: AgentSpec, request: AgentRequest) -> HarnessContext:
        self._run_counter += 1
        run_id = f"{request.session_id}-{self._run_counter}"
        return HarnessContext(
            session_id=request.session_id,
            agent_name=spec.component.name,
            cwd=self.cwd,
            model=self.model,
            run_id=run_id,
            permission=PermissionContext(mode="default", session_id=request.session_id),
            state_repository=self.state_repository,
            active_context_store=self.active_context_store,
            history_mirror=self.history_mirror,
            memory_injector_registry=self.memory_injector_registry,
            tool_registry=self.tool_registry,
            skill_registry=self.skill_registry,
            model_registry=self.model_registry,
            config_snapshot={"provider": self.provider},
        )


class DefaultAgentEventMapper(AgentEventMapper):
    async def map(self, session_id: str, event: HarnessEvent) -> AgentEvent:
        kind = "final" if event.kind == "final" else ("error" if event.kind == "error" else "draft")
        return AgentEvent(kind=kind, session_id=session_id, payload=dict(event.payload), harness_event=event)


class ChatAgent(Agent):
    def __init__(
        self,
        *,
        spec: AgentSpec,
        harness: Harness,
        active_context_store: ActiveContextStore,
        history_mirror: HistoryMirror,
        memory_injector_registry: MemoryInjectorRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        model_registry: ModelRegistry,
        state_repository: StateRepository,
        cwd: str,
        model: str,
        provider: str,
    ) -> None:
        self._spec = spec
        self.harness = harness
        self.request_mapper = DefaultAgentRequestMapper(
            active_context_store=active_context_store,
            history_mirror=history_mirror,
            memory_injector_registry=memory_injector_registry,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            model_registry=model_registry,
            state_repository=state_repository,
            cwd=cwd,
            model=model,
            provider=provider,
        )
        self.event_mapper = DefaultAgentEventMapper()

    def spec(self) -> AgentSpec:
        return self._spec

    async def send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        async for event in self._send(request):
            yield event

    async def _send(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        harness_request = await self.request_mapper.to_harness_request(self._spec, request)
        harness_context = await self.request_mapper.to_harness_context(self._spec, request)
        async for harness_event in self.harness.invoke(harness_request, harness_context):
            yield await self.event_mapper.map(request.session_id, harness_event)

    async def control(self, request: AgentControlRequest) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            kind="control_result",
            session_id=request.session_id,
            payload={"op": request.op, "ok": False, "reason": "not implemented"},
        )

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        return None
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_chat_agent.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/agents/chat_agent.py tests/test_chat_agent.py
git commit -m "$(cat <<'EOF'
feat(agents): ChatAgent + DefaultAgentRequestMapper + DefaultAgentEventMapper

ChatAgent.send delegates to DefaultHarness via injected mapper. Event
mapper translates HarnessEvent kinds: final->final, error->error,
everything else->draft.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 12: DefaultAgentRequestRouter + DefaultAgentGateway

**Files:**
- Create: `src/xana/agent_adapter/router.py`
- Create: `src/xana/agent_adapter/gateway.py`
- Test: `tests/test_default_gateway.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_default_gateway.py`. The test embeds a `_StubAdapter` because `CliAdapter` is implemented in Task 13:

```python
from __future__ import annotations

from typing import AsyncIterator

import pytest

from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.interfaces import Adapter
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.contracts import AgentEvent
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.skills.builtin.identity import IdentitySkill
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.tools.registry import InMemoryToolRegistry


class _StubAdapter(Adapter):
    """In-test stub. CliAdapter lands in Task 13; the gateway test only needs render()."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def receive(self) -> AsyncIterator[InboundMessage]:
        if False:
            yield  # type: ignore[unreachable]

    async def render(self, event: AgentEvent) -> RenderedMessage:
        return RenderedMessage(
            adapter_id="cli",
            session_id=event.session_id,
            text=event.payload.get("text", ""),
            metadata={"harness_kind": event.harness_event.kind if event.harness_event else None},
        )

    async def bind_session(self, binding: SessionBinding) -> None: ...

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id="cli", status="healthy")


@pytest.mark.asyncio
async def test_router_maps_inbound_to_agent_request_with_default_target() -> None:
    router = DefaultAgentRequestRouter(default_agent="chat")
    inbound = InboundMessage(adapter_id="cli", session_id="s1", text="hi")
    req = await router.route(inbound)
    assert req.target_agent == "chat"
    assert req.text == "hi"
    assert req.session_id == "s1"
    assert req.source == "cli"


async def _build_gateway() -> DefaultAgentGateway:
    skills = InMemorySkillRegistry()
    await skills.register(IdentitySkill())
    model_registry = InMemoryModelRegistry()
    await model_registry.register(
        "fake",
        FakeModelClient(ScriptedTurn(text_deltas=("hi",), usage_input_tokens=1, usage_output_tokens=1)),
    )
    agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(),
        active_context_store=InMemoryActiveContextStore(),
        history_mirror=InMemoryHistoryMirror(),
        memory_injector_registry=DefaultMemoryInjectorRegistry(),
        tool_registry=InMemoryToolRegistry(),
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=InMemoryStateRepository(),
        cwd="/tmp",
        model="fake-model",
        provider="fake",
    )
    gateway = DefaultAgentGateway(
        agents={"chat": agent},
        router=DefaultAgentRequestRouter(default_agent="chat"),
    )
    await gateway.register_adapter("cli", _StubAdapter())
    return gateway


@pytest.mark.asyncio
async def test_gateway_submit_yields_rendered_messages_with_final_at_end() -> None:
    gateway = await _build_gateway()
    inbound = InboundMessage(adapter_id="cli", session_id="s1", text="hi")
    rendered = [r async for r in gateway.submit(inbound)]
    assert rendered, "expected at least one rendered message"
    last = rendered[-1]
    assert last.metadata.get("event_kind") == "final"


@pytest.mark.asyncio
async def test_gateway_attach_binds_session_metadata() -> None:
    gateway = await _build_gateway()
    binding = SessionBinding(adapter_id="cli", external_thread_id="tty", session_id="s1")
    await gateway.attach(binding)
    assert gateway.bindings["s1"] == binding
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_default_gateway.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement router**

Create `src/xana/agent_adapter/router.py`:

```python
"""DefaultAgentRequestRouter: InboundMessage -> AgentRequest.

v1 routes everything to a single default agent. Future versions can
parse `/agent_name` prefixes or look up by adapter/channel.
"""
from __future__ import annotations

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.interfaces import AgentRequestRouter
from xana.agents.contracts import AgentRequest


class DefaultAgentRequestRouter(AgentRequestRouter):
    def __init__(self, default_agent: str) -> None:
        self.default_agent = default_agent

    async def route(self, inbound: InboundMessage) -> AgentRequest:
        return AgentRequest(
            session_id=inbound.session_id,
            text=inbound.text,
            source=inbound.adapter_id,
            target_agent=self.default_agent,
            attachments=inbound.attachments,
            metadata=dict(inbound.metadata),
        )
```

- [ ] **Step 4: Implement gateway**

Create `src/xana/agent_adapter/gateway.py`:

```python
"""DefaultAgentGateway: wires Adapter <-> Agent."""
from __future__ import annotations

from typing import AsyncIterator

from xana.agent_adapter.contracts import (
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import (
    Adapter,
    AgentGateway,
    AgentRequestRouter,
    CommandRouter,
)
from xana.agents.contracts import AgentEvent
from xana.agents.interfaces import Agent


class DefaultAgentGateway(AgentGateway):
    def __init__(
        self,
        agents: dict[str, Agent],
        router: AgentRequestRouter,
    ) -> None:
        self.agents = agents
        self.router = router
        self.adapters: dict[str, Adapter] = {}
        self.command_router: CommandRouter | None = None
        self.bindings: dict[str, SessionBinding] = {}

    async def register_adapter(self, adapter_id: str, adapter: Adapter) -> None:
        self.adapters[adapter_id] = adapter

    async def register_command_router(self, router: CommandRouter) -> None:
        self.command_router = router

    async def attach(self, binding: SessionBinding) -> None:
        self.bindings[binding.session_id] = binding

    async def submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        async for rendered in self._submit(inbound):
            yield rendered

    async def _submit(self, inbound: InboundMessage) -> AsyncIterator[RenderedMessage]:
        request = await self.router.route(inbound)
        target = request.target_agent or ""
        agent = self.agents.get(target)
        adapter = self.adapters.get(inbound.adapter_id)
        if agent is None or adapter is None:
            yield RenderedMessage(
                adapter_id=inbound.adapter_id,
                session_id=inbound.session_id,
                text=f"[gateway] unknown agent={target!r} or adapter={inbound.adapter_id!r}",
                metadata={"event_kind": "error"},
            )
            return
        async for event in agent.send(request):
            rendered = await adapter.render(event)
            yield self._attach_event_kind(rendered, event)

    def subscribe(self, adapter_id: str, session_id: str) -> AsyncIterator[RenderedMessage]:
        raise NotImplementedError("subscribe is not used by the CLI flow in v1")

    async def cancel(self, session_id: str, run_id: str | None = None) -> None:
        for agent in self.agents.values():
            await agent.cancel(session_id, run_id)

    @staticmethod
    def _attach_event_kind(rendered: RenderedMessage, event: AgentEvent) -> RenderedMessage:
        metadata = dict(rendered.metadata)
        metadata.setdefault("event_kind", event.kind)
        return RenderedMessage(
            adapter_id=rendered.adapter_id,
            session_id=rendered.session_id,
            text=rendered.text,
            draft=rendered.draft,
            metadata=metadata,
        )
```

- [ ] **Step 5: Run tests to confirm pass**

Run: `python -m pytest tests/test_default_gateway.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/router.py src/xana/agent_adapter/gateway.py tests/test_default_gateway.py
git commit -m "$(cat <<'EOF'
feat(agent_adapter): DefaultAgentRequestRouter + DefaultAgentGateway

Router maps InboundMessage to AgentRequest with a default target agent.
Gateway wires adapters -> agent.send -> adapter.render, attaches
event_kind metadata to rendered messages so adapters can distinguish
draft vs final.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 13: CliAdapter

**Files:**
- Create: `src/xana/agent_adapter/cli.py`
- Test: `tests/test_cli_adapter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_adapter.py`:

```python
from __future__ import annotations

import asyncio
import io

import pytest

from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.contracts import InboundMessage
from xana.agents.contracts import AgentEvent


@pytest.mark.asyncio
async def test_render_writes_payload_text_to_buffer() -> None:
    buf = io.StringIO()
    adapter = CliAdapter(out=buf, adapter_id="cli", session_id="s1")
    rendered = await adapter.render(
        AgentEvent(kind="draft", session_id="s1", payload={"text": "abc"})
    )
    assert rendered.text == "abc"
    assert "abc" in buf.getvalue()


@pytest.mark.asyncio
async def test_render_final_event_sets_turn_complete_event() -> None:
    buf = io.StringIO()
    adapter = CliAdapter(out=buf, adapter_id="cli", session_id="s1")
    assert not adapter.turn_complete.is_set()
    await adapter.render(AgentEvent(kind="final", session_id="s1", payload={"text": "DONE"}))
    assert adapter.turn_complete.is_set()
    assert "DONE" in buf.getvalue()


@pytest.mark.asyncio
async def test_submit_and_receive_round_trips_inbound() -> None:
    buf = io.StringIO()
    adapter = CliAdapter(out=buf, adapter_id="cli", session_id="s1")
    await adapter.submit("hello")
    received: list[InboundMessage] = []
    async for inbound in adapter.receive():
        received.append(inbound)
        break
    assert len(received) == 1
    assert received[0].text == "hello"
    assert received[0].session_id == "s1"


@pytest.mark.asyncio
async def test_health_reports_healthy_after_start() -> None:
    adapter = CliAdapter(out=io.StringIO(), adapter_id="cli", session_id="s1")
    await adapter.start()
    health = await adapter.health()
    assert health.status == "healthy"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_cli_adapter.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/agent_adapter/cli.py`:

```python
"""CliAdapter: stdin/stdout adapter for yuxuc.

Pattern (after yuxu): inbound messages are queued in via submit(); each call
to receive() yields exactly one queued message. render() writes the event
payload to the output stream and signals turn_complete when kind=="final".
"""
from __future__ import annotations

import asyncio
import sys
from typing import AsyncIterator, TextIO

from xana.agent_adapter.contracts import (
    AdapterHealth,
    InboundMessage,
    RenderedMessage,
    SessionBinding,
)
from xana.agent_adapter.interfaces import Adapter
from xana.agents.contracts import AgentEvent


class CliAdapter(Adapter):
    def __init__(
        self,
        *,
        out: TextIO | None = None,
        adapter_id: str = "cli",
        session_id: str = "cli-session",
    ) -> None:
        self.out = out if out is not None else sys.stdout
        self.adapter_id = adapter_id
        self.session_id = session_id
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.turn_complete: asyncio.Event = asyncio.Event()
        self._status: str = "stopped"
        self._bindings: list[SessionBinding] = []

    async def start(self) -> None:
        self._status = "healthy"

    async def stop(self) -> None:
        self._status = "stopped"

    async def submit(self, text: str) -> None:
        self.turn_complete.clear()
        await self._inbound.put(
            InboundMessage(
                adapter_id=self.adapter_id,
                session_id=self.session_id,
                text=text,
            )
        )

    async def receive(self) -> AsyncIterator[InboundMessage]:
        msg = await self._inbound.get()
        yield msg

    async def render(self, event: AgentEvent) -> RenderedMessage:
        text = str(event.payload.get("text", ""))
        if text:
            self.out.write(text)
            if not text.endswith("\n"):
                self.out.write("\n")
            self.out.flush()
        if event.kind in ("final", "error"):
            self.turn_complete.set()
        return RenderedMessage(
            adapter_id=self.adapter_id,
            session_id=event.session_id,
            text=text,
            metadata={"event_kind": event.kind},
        )

    async def bind_session(self, binding: SessionBinding) -> None:
        self._bindings.append(binding)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, status=self._status)  # type: ignore[arg-type]
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_cli_adapter.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/cli.py tests/test_cli_adapter.py
git commit -m "$(cat <<'EOF'
feat(agent_adapter): CliAdapter — stdin/stdout adapter for yuxuc

submit() queues inbound, receive() yields one queued InboundMessage per
call, render() writes event text and signals turn_complete on final.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 14: runtime.boot.boot()

**Files:**
- Create: `src/xana/runtime/boot.py`
- Test: `tests/test_runtime_boot.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_boot.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot


@pytest.mark.asyncio
async def test_boot_returns_gateway_and_registries(tmp_path: Path) -> None:
    (tmp_path / "README_ai.md").write_text("---\ntitle: Test Memory\n---\n# Test\nbody content\n", encoding="utf-8")
    result = await boot(project_root=tmp_path)
    assert result.gateway is not None
    assert "chat" in result.agents
    assert await result.tool_registry.get("memory_search") is not None
    assert await result.skill_registry.get("identity") is not None


@pytest.mark.asyncio
async def test_boot_loads_readme_into_memory_tree(tmp_path: Path) -> None:
    (tmp_path / "README_ai.md").write_text("---\ntitle: Hello\n---\n# Heading\nfoo\n", encoding="utf-8")
    result = await boot(project_root=tmp_path)
    node = await result.memory_tree.get_node("README_ai.md")
    assert node is not None
    assert "foo" in node.body


@pytest.mark.asyncio
async def test_boot_search_returns_hits_for_indexed_content(tmp_path: Path) -> None:
    (tmp_path / "README_ai.md").write_text("---\ntitle: Doc\n---\n# Title\nauthentication code\n", encoding="utf-8")
    result = await boot(project_root=tmp_path)
    from xana.memory.contracts import SearchQuery
    hits = await result.search_engine.search(SearchQuery(text="authentication", top_k=5))
    assert hits
    assert hits[0].node_path == "README_ai.md"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_runtime_boot.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/xana/runtime/boot.py`:

```python
"""boot(): wire up the first core flow in one place.

Returns a BootResult holding the AgentGateway and all the registries
under it, so the CLI and tests can introspect.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.router import DefaultAgentRequestRouter
from xana.agents.chat_agent import ChatAgent, build_chat_agent_spec
from xana.agents.interfaces import Agent
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.llm_driver.interfaces import ModelRegistry
from xana.memory.active_context import (
    DefaultMemoryInjectorRegistry,
    InMemoryActiveContextStore,
    InMemoryHistoryMirror,
)
from xana.memory.interfaces import (
    ActiveContextStore,
    HistoryMirror,
    MemoryInjectorRegistry,
    MemorySearchEngine,
    MemoryTree,
)
from xana.memory.markdown import (
    MarkdownMemoryTree,
    SQLiteMemorySearchEngine,
    SearchMemoryInjector,
)
from xana.skills.builtin.identity import IdentitySkill
from xana.skills.interfaces import SkillRegistry
from xana.skills.registry import InMemorySkillRegistry
from xana.state.in_memory import InMemoryStateRepository
from xana.state.interfaces import StateRepository
from xana.tools.builtin.memory_search import MemorySearchTool
from xana.tools.interfaces import ToolRegistry
from xana.tools.registry import InMemoryToolRegistry


@dataclass
class BootResult:
    gateway: DefaultAgentGateway
    agents: dict[str, Agent]
    memory_tree: MemoryTree
    search_engine: MemorySearchEngine
    active_context_store: ActiveContextStore
    history_mirror: HistoryMirror
    memory_injector_registry: MemoryInjectorRegistry
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    model_registry: ModelRegistry
    state_repository: StateRepository


async def boot(
    *,
    project_root: Path,
    model: str = "fake-model",
    provider: str = "fake",
    scripted_reply: str = "Hello from xana.",
) -> BootResult:
    project_root = Path(project_root).resolve()

    tree = MarkdownMemoryTree(root=project_root, entrypoint="README_ai.md")
    await tree.load()
    engine = SQLiteMemorySearchEngine(":memory:")
    await engine.rebuild_from_tree(tree)
    injector = SearchMemoryInjector(engine, top_k=3, budget_chars=2000)
    injector_registry = DefaultMemoryInjectorRegistry()
    await injector_registry.register(injector, priority=100)

    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()

    tools = InMemoryToolRegistry()
    await tools.register(MemorySearchTool(engine))

    skills = InMemorySkillRegistry()
    await skills.register(IdentitySkill())

    model_registry = InMemoryModelRegistry()
    await model_registry.register(
        provider,
        FakeModelClient(
            ScriptedTurn(
                text_deltas=(scripted_reply,),
                tool_call=None,
                usage_input_tokens=4,
                usage_output_tokens=8,
            )
        ),
    )

    state = InMemoryStateRepository()

    chat_agent = ChatAgent(
        spec=build_chat_agent_spec(),
        harness=DefaultHarness(),
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=state,
        cwd=str(project_root),
        model=model,
        provider=provider,
    )

    gateway = DefaultAgentGateway(
        agents={"chat": chat_agent},
        router=DefaultAgentRequestRouter(default_agent="chat"),
    )

    return BootResult(
        gateway=gateway,
        agents={"chat": chat_agent},
        memory_tree=tree,
        search_engine=engine,
        active_context_store=active_ctx,
        history_mirror=mirror,
        memory_injector_registry=injector_registry,
        tool_registry=tools,
        skill_registry=skills,
        model_registry=model_registry,
        state_repository=state,
    )
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `python -m pytest tests/test_runtime_boot.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xana/runtime/boot.py tests/test_runtime_boot.py
git commit -m "$(cat <<'EOF'
feat(runtime): boot() wires the first core flow

Loads README_ai.md via MarkdownMemoryTree, indexes into
SQLiteMemorySearchEngine, registers SearchMemoryInjector,
memory_search tool, IdentitySkill, FakeModelClient, ChatAgent,
DefaultAgentRequestRouter, DefaultAgentGateway. Returns a BootResult
exposing every store/registry for CLI and test inspection.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 15: yuxuc CLI entry point

**Files:**
- Create: `src/xana/cli/__init__.py`
- Create: `src/xana/cli/yuxuc.py`

- [ ] **Step 1: Implement CLI module**

Create `src/xana/cli/__init__.py`:

```python
"""xana CLIs."""
```

Create `src/xana/cli/yuxuc.py`:

```python
"""yuxuc: xana's CLI client.

Boots the runtime in-process, registers a CliAdapter, sends the user's
message through the AgentGateway, prints streamed events to stdout,
exits when the turn completes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from xana.agent_adapter.cli import CliAdapter
from xana.agent_adapter.contracts import InboundMessage
from xana.runtime.boot import boot


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="yuxuc", description="xana CLI client")
    parser.add_argument("text", nargs="*", help="message to send (positional)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: cwd)")
    parser.add_argument("--session", default="cli-session", help="session id")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    project_root = (args.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    adapter = CliAdapter(adapter_id="cli", session_id=args.session)
    await adapter.start()
    await boot_result.gateway.register_adapter("cli", adapter)

    text = " ".join(args.text).strip()
    if not text:
        print("[yuxuc] (empty input — pass text positionally)", file=sys.stderr)
        return 2

    inbound = InboundMessage(adapter_id="cli", session_id=args.session, text=text)
    exit_code = 0
    async for rendered in boot_result.gateway.submit(inbound):
        if rendered.metadata.get("event_kind") == "error":
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-run via python -m**

Run: `cd /home/xzp/project/xana && python -m xana.cli.yuxuc "hello world"`
Expected: stdout contains the scripted "Hello from xana." reply. Exit code 0.

- [ ] **Step 3: Commit**

```bash
git add src/xana/cli/__init__.py src/xana/cli/yuxuc.py
git commit -m "$(cat <<'EOF'
feat(cli): yuxuc — one-shot CLI client

Boots runtime in-process, registers CliAdapter, sends user message
through AgentGateway, streams events to stdout, exits at final.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 16: README_ai.md memory mount

**Files:**
- Modify: `README_ai.md`

- [ ] **Step 1: Update README_ai.md to mount runtime entry**

Edit `README_ai.md`. Under "## 项目入口" add a new bullet at the top:

```markdown
- **Runtime entry**：[cli.yuxuc](src/xana/cli/yuxuc.py) — 一进程 CLI 入口；`boot(project_root)` 装好整个第一核心流（gateway/agent/harness/tool/skill/memory）
- **第一核心流 spec**：[docs/superpowers/specs/2026-06-11-first-core-flow-design.md](docs/superpowers/specs/2026-06-11-first-core-flow-design.md)
- **第一核心流 plan**：[docs/superpowers/plans/2026-06-11-first-core-flow.md](docs/superpowers/plans/2026-06-11-first-core-flow.md)
```

Place these three bullets immediately after the existing "- **模块设计总览**" line.

- [ ] **Step 2: Verify memory tree still loads cleanly**

Run: `python -m pytest tests/test_runtime_boot.py -v`
Expected: still 3 passed (the README_ai.md changes don't break loading).

- [ ] **Step 3: Commit**

```bash
git add README_ai.md
git commit -m "$(cat <<'EOF'
docs(memory): mount runtime entry and first-core-flow spec/plan in README_ai.md

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 17: End-to-end subprocess test

**Files:**
- Create: `tests/test_yuxuc_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_yuxuc_e2e.py`:

```python
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["HOME"] = str(tmp_path)
    return env


def test_yuxuc_oneshot_prints_scripted_reply(tmp_path: Path) -> None:
    """`python -m xana.cli.yuxuc --project DIR 'hello'` prints the scripted reply and exits 0."""
    (tmp_path / "README_ai.md").write_text(
        "---\ntitle: Memory Root\n---\n# memory\nsample body\n", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-m", "xana.cli.yuxuc", "--project", str(tmp_path), "hello"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
        env=_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "Hello from xana" in result.stdout


def test_yuxuc_empty_input_exits_nonzero(tmp_path: Path) -> None:
    (tmp_path / "README_ai.md").write_text("# empty\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "xana.cli.yuxuc", "--project", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(tmp_path),
        env=_env(tmp_path),
    )
    assert result.returncode != 0
    assert "empty input" in result.stderr
```

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_yuxuc_e2e.py -v`
Expected: 2 passed.

- [ ] **Step 3: Run full test suite to confirm green**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (~100+ tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_yuxuc_e2e.py
git commit -m "$(cat <<'EOF'
test(cli): end-to-end subprocess smoke for yuxuc

Asserts `python -m xana.cli.yuxuc --project DIR "hello"` prints the
scripted reply and exits 0; empty input exits nonzero.

Generated with [Claude Code](https://claude.ai/code)
via [Happy](https://happy.engineering)

Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Happy <yesreply@happy.engineering>
EOF
)"
```

---

## Task 18: Final verification

**Files:** (none — sanity)

- [ ] **Step 1: Run full suite**

Run: `python -m pytest tests/ -v 2>&1 | tail -40`
Expected: all green, no skips.

- [ ] **Step 2: Verify git state**

Run: `git log --oneline`
Expected: ≥ 18 commits, each labeled correctly.

Run: `git status`
Expected: working tree clean.

Run: `git remote -v`
Expected: shows `origin git@github.com:aicodex/xana.git` for both fetch and push.

- [ ] **Step 3: Confirm no push**

DO NOT run `git push`. The user explicitly requested local commits only.

- [ ] **Step 4: Smoke the CLI one final time**

Run from project root: `python -m xana.cli.yuxuc "what is xana"`
Expected: prints `Hello from xana.` followed by exit 0.

---

## Spec coverage check

- ✅ yuxuc → gateway → harness → tool → skill flow: Tasks 12–15
- ✅ Memory integration via MarkdownMemoryTree + Search + Injector: Tasks 2, 8, 14
- ✅ Strict interface conformance: every concrete class subclasses an existing ABC
- ✅ README_ai.md memory mount: Task 16
- ✅ git init + remote, no push: Tasks 0, 18
- ✅ All canonical tests still green: verified in Task 7's Step 5, Task 10's Step 5, Task 17's Step 3
- ✅ FakeModelClient instead of real LLM: Task 7
- ✅ memory_search tool: Task 8
- ✅ IdentitySkill: Task 9
- ✅ DefaultHarness owns turn lifecycle: Task 10
- ✅ Subprocess E2E proves CLI works: Task 17
