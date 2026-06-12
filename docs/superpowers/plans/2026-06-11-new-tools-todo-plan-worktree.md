# New Tools: TodoWrite, EnterPlanMode, EnterWorktree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three CC-parity tools to xana's built-in registry. Each is a thin Tool subclass following the existing pattern (see `src/xana/tools/builtin/write.py`):
- **TodoWrite** — stores a per-session todo list as JSON under `~/.xana/sessions/{session_id}.todos.json`.
- **EnterPlanMode** — flips `PermissionContext.mode` to `"plan"` for the rest of the session via `SessionConfig.metadata`.
- **EnterWorktree** — creates a `git worktree` under `~/.xana/worktrees/{session_id}/{slug}`, persists the path on `SessionConfig.metadata`.

**Architecture:**
- Each tool lives in `src/xana/tools/builtin/`. None require interface changes — `ToolSpec`/`ToolResult` cover everything.
- TodoWrite stores state in a JSON sidecar (similar shape to `SessionConfig` but separate file). The tool body owns its persistence — no `ActiveContextStore` injection required (matches cc's approach of stashing todos in `appState.todos`).
- EnterPlanMode + EnterWorktree both mutate `SessionConfig.metadata` so the change survives across turns. They read/write via a `SessionConfigStore` reference passed at construction. If Plan 1's `SessionConfigStore` hasn't shipped, the tools degrade to a no-op flag stored in memory (single-process semantics only) — design discussed in tasks 2/4.
- All three are registered in `src/xana/runtime/boot.py` next to existing tool registrations.

**Tech Stack:** Python 3.10+, existing `Tool` interface, optional `SessionConfigStore` (Plan 1).

**File structure:**
- Create: `src/xana/tools/builtin/todo_write.py`
- Create: `src/xana/tools/builtin/enter_plan_mode.py`
- Create: `src/xana/tools/builtin/enter_worktree.py`
- Create: `src/xana/tools/builtin/_todo_store.py` (JSON I/O for TodoWrite)
- Modify: `src/xana/runtime/boot.py`
- Test: `tests/test_todo_write_tool.py`
- Test: `tests/test_enter_plan_mode_tool.py`
- Test: `tests/test_enter_worktree_tool.py`
- Test: `tests/test_boot_registers_new_tools.py`

---

### Task 1: TodoStore helper (JSON sidecar)

**Files:**
- Create: `src/xana/tools/builtin/_todo_store.py`
- Test: `tests/test_todo_store.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_todo_store.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.tools.builtin._todo_store import TodoItem, TodoStore


def test_load_unknown_session_returns_empty(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    assert store.load("never-seen") == ()


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    items = (
        TodoItem(content="finish plan", active_form="Finishing plan", status="in_progress"),
        TodoItem(content="ship it", active_form="Shipping", status="pending"),
    )
    store.save("s1", items)
    loaded = store.load("s1")
    assert loaded == items


def test_save_overwrites_previous(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    store.save("s1", (TodoItem(content="a", active_form="A", status="pending"),))
    store.save("s1", (TodoItem(content="b", active_form="B", status="completed"),))
    loaded = store.load("s1")
    assert len(loaded) == 1 and loaded[0].content == "b"


def test_save_rejects_unsafe_session_id(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.save("../etc/passwd", ())


def test_save_atomic_uses_tmp_then_rename(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    store.save("s1", (TodoItem(content="x", active_form="X", status="pending"),))
    # No leftover .tmp file
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_todo_store.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement TodoStore**

Create `src/xana/tools/builtin/_todo_store.py`:

```python
"""Per-session todo storage. JSON file at ~/.xana/sessions/{sid}.todos.json.

Mirrors cc's TodoWrite semantics: a single overwritable list per session.
TodoItem fields are content (imperative), active_form (present continuous),
and status (pending / in_progress / completed). Status semantics match cc.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass(frozen=True)
class TodoItem:
    content: str
    active_form: str
    status: TodoStatus


class TodoStore:
    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id.split("/"):
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self.root / f"{session_id}.todos.json"

    def load(self, session_id: str) -> tuple[TodoItem, ...]:
        path = self._path(session_id)
        if not path.exists():
            return ()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return tuple(TodoItem(**item) for item in raw)

    def save(self, session_id: str, items: tuple[TodoItem, ...]) -> None:
        path = self._path(session_id)
        tmp = path.with_suffix(".todos.json.tmp")
        payload = [dataclasses.asdict(i) for i in items]
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_todo_store.py -v`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/tools/builtin/_todo_store.py tests/test_todo_store.py
git commit -m "feat(tools): TodoStore JSON sidecar for per-session todos"
```

---

### Task 2: TodoWrite tool

**Files:**
- Create: `src/xana/tools/builtin/todo_write.py`
- Test: `tests/test_todo_write_tool.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_todo_write_tool.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.policy.contracts import PermissionContext
from xana.tools.builtin.todo_write import TodoWriteTool
from xana.tools.builtin._todo_store import TodoStore
from xana.tools.contracts import ToolContext, ToolInvocation


def _ctx(session_id: str = "s1", cwd: str = "/") -> ToolContext:
    return ToolContext(session_id=session_id, agent_name="chat", cwd=cwd,
                       permission=PermissionContext(mode="default", session_id=session_id))


@pytest.mark.asyncio
async def test_spec_name_and_required_inputs() -> None:
    tool = TodoWriteTool(TodoStore(root=Path("/tmp")))
    spec = tool.spec()
    assert spec.component.name == "TodoWrite"
    assert "todos" in spec.input_schema["properties"]
    assert spec.input_schema.get("required") == ["todos"]


@pytest.mark.asyncio
async def test_execute_saves_todos_to_store(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    tool = TodoWriteTool(store)
    inv = ToolInvocation(
        tool_name="TodoWrite",
        input={
            "todos": [
                {"content": "Write code", "active_form": "Writing code", "status": "in_progress"},
                {"content": "Run tests", "active_form": "Running tests", "status": "pending"},
            ],
        },
        call_id="c1", session_id="s1",
    )
    await tool.validate(inv)
    result = await tool.execute(inv, _ctx())
    assert result.ok is True
    saved = store.load("s1")
    assert len(saved) == 2 and saved[0].content == "Write code"


@pytest.mark.asyncio
async def test_execute_rejects_bad_status(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    tool = TodoWriteTool(store)
    inv = ToolInvocation(
        tool_name="TodoWrite",
        input={"todos": [{"content": "x", "active_form": "X", "status": "wat"}]},
        call_id="c1", session_id="s1",
    )
    with pytest.raises(ValueError):
        await tool.validate(inv)


@pytest.mark.asyncio
async def test_execute_with_empty_todos_clears_session(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    tool = TodoWriteTool(store)
    inv = ToolInvocation(tool_name="TodoWrite", input={"todos": []},
                          call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx())
    assert result.ok is True
    assert store.load("s1") == ()


@pytest.mark.asyncio
async def test_rendered_summarizes_counts(tmp_path: Path) -> None:
    store = TodoStore(root=tmp_path)
    tool = TodoWriteTool(store)
    inv = ToolInvocation(
        tool_name="TodoWrite",
        input={"todos": [
            {"content": "a", "active_form": "A", "status": "completed"},
            {"content": "b", "active_form": "B", "status": "pending"},
            {"content": "c", "active_form": "C", "status": "in_progress"},
        ]},
        call_id="c1", session_id="s1",
    )
    result = await tool.execute(inv, _ctx())
    assert "1" in (result.rendered or "")  # at least one count visible
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_todo_write_tool.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement TodoWriteTool**

Create `src/xana/tools/builtin/todo_write.py`:

```python
"""TodoWrite: per-session task list management.

Mirrors cc's TodoWriteTool semantics. Each call OVERWRITES the session's
list; the model is responsible for sending the full snapshot every time.
We persist via TodoStore so the list survives REPL restarts.
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._todo_store import TodoItem, TodoStore
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_VALID_STATUS = {"pending", "in_progress", "completed"}

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="TodoWrite",
        qualified_name="builtin.TodoWrite",
        description="Create/replace the session's structured todo list. Send the full list every call.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content":     {"type": "string"},
                        "active_form": {"type": "string"},
                        "status":      {"type": "string", "enum": sorted(_VALID_STATUS)},
                    },
                    "required": ["content", "active_form", "status"],
                },
            },
        },
        "required": ["todos"],
    },
)


class TodoWriteTool(Tool):
    def __init__(self, store: TodoStore) -> None:
        self.store = store

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        todos = invocation.input.get("todos")
        if not isinstance(todos, list):
            raise ValueError("TodoWrite: 'todos' must be a list")
        for i, t in enumerate(todos):
            if not isinstance(t, dict):
                raise ValueError(f"TodoWrite: item {i} must be an object")
            if t.get("status") not in _VALID_STATUS:
                raise ValueError(f"TodoWrite: item {i} has invalid status {t.get('status')!r}")
            for k in ("content", "active_form"):
                if not isinstance(t.get(k), str) or not t[k]:
                    raise ValueError(f"TodoWrite: item {i} missing/empty '{k}'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="todo write allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        items = tuple(
            TodoItem(content=t["content"], active_form=t["active_form"], status=t["status"])
            for t in invocation.input["todos"]
        )
        self.store.save(context.session_id, items)
        counts = {s: sum(1 for i in items if i.status == s) for s in _VALID_STATUS}
        rendered = (
            f"[TodoWrite ok] {len(items)} item(s): "
            f"{counts['in_progress']} in-progress, "
            f"{counts['pending']} pending, "
            f"{counts['completed']} completed"
        )
        return ToolResult(ok=True, content=[dict(c=i.content, s=i.status) for i in items], rendered=rendered)

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_todo_write_tool.py -v`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/tools/builtin/todo_write.py tests/test_todo_write_tool.py
git commit -m "feat(tools): TodoWrite — per-session JSON todo list (cc parity)"
```

---

### Task 3: EnterPlanMode tool

**Files:**
- Create: `src/xana/tools/builtin/enter_plan_mode.py`
- Test: `tests/test_enter_plan_mode_tool.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_enter_plan_mode_tool.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.policy.contracts import PermissionContext
from xana.session_config.contracts import SessionConfig
from xana.session_config.file_store import FileSessionConfigStore
from xana.tools.builtin.enter_plan_mode import EnterPlanModeTool
from xana.tools.contracts import ToolContext, ToolInvocation


def _ctx(session_id: str = "s1") -> ToolContext:
    return ToolContext(session_id=session_id, agent_name="chat", cwd="/",
                       permission=PermissionContext(mode="default", session_id=session_id))


@pytest.mark.asyncio
async def test_spec_has_empty_input_schema() -> None:
    tool = EnterPlanModeTool(session_config_store=None)
    spec = tool.spec()
    assert spec.component.name == "EnterPlanMode"
    assert spec.input_schema.get("properties", {}) in ({}, None)


@pytest.mark.asyncio
async def test_execute_persists_plan_mode_flag(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    tool = EnterPlanModeTool(session_config_store=store)
    inv = ToolInvocation(tool_name="EnterPlanMode", input={}, call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx())
    assert result.ok is True
    cfg = await store.get("s1")
    assert cfg.metadata.get("mode") == "plan"


@pytest.mark.asyncio
async def test_execute_without_store_uses_metadata_marker_in_result() -> None:
    tool = EnterPlanModeTool(session_config_store=None)
    inv = ToolInvocation(tool_name="EnterPlanMode", input={}, call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx())
    assert result.ok is True
    assert result.metadata.get("mode") == "plan"


@pytest.mark.asyncio
async def test_render_result_describes_plan_mode() -> None:
    tool = EnterPlanModeTool(session_config_store=None)
    inv = ToolInvocation(tool_name="EnterPlanMode", input={}, call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx())
    rendered = await tool.render_result(result)
    assert "plan" in rendered.lower()
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_enter_plan_mode_tool.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement EnterPlanModeTool**

Create `src/xana/tools/builtin/enter_plan_mode.py`:

```python
"""EnterPlanMode: flip the session into 'plan' mode.

Plan mode (cc): the agent should focus on exploration and design, NOT
execute mutating tools. We persist the marker on SessionConfig.metadata
so the next turn's HarnessContext can refuse destructive Tool.execute()
calls. Tools opt in by checking PermissionContext.mode.

If no SessionConfigStore is wired, the tool still returns success but the
marker only lives in the result.metadata — single-turn-only effect.
"""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.session_config.contracts import SessionConfig
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="EnterPlanMode",
        qualified_name="builtin.EnterPlanMode",
        description="Switch the session into plan mode (exploration + design, no destructive tools).",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    read_only=True,
)


class EnterPlanModeTool(Tool):
    def __init__(self, *, session_config_store) -> None:
        self.session_config_store = session_config_store

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        return None

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="plan-mode toggle allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        if self.session_config_store is not None:
            current = await self.session_config_store.get(context.session_id)
            new_metadata = dict(current.metadata)
            new_metadata["mode"] = "plan"
            await self.session_config_store.save(SessionConfig(
                session_id=current.session_id, model=current.model, effort=current.effort,
                streaming=current.streaming, thinking=current.thinking,
                search_provider=current.search_provider, project_root=current.project_root,
                metadata=new_metadata,
            ))
        return ToolResult(
            ok=True,
            content={"mode": "plan"},
            rendered="[EnterPlanMode] 已进入 plan 模式。后续不要执行修改性工具，专注于探索和设计。",
            metadata={"mode": "plan"},
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_enter_plan_mode_tool.py -v`
Expected: 4 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/tools/builtin/enter_plan_mode.py tests/test_enter_plan_mode_tool.py
git commit -m "feat(tools): EnterPlanMode persists plan-mode flag to SessionConfig.metadata"
```

---

### Task 4: EnterWorktree tool

**Files:**
- Create: `src/xana/tools/builtin/enter_worktree.py`
- Test: `tests/test_enter_worktree_tool.py`

- [x] **Step 1: Write failing tests**

Create `tests/test_enter_worktree_tool.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from xana.policy.contracts import PermissionContext
from xana.session_config.file_store import FileSessionConfigStore
from xana.tools.builtin.enter_worktree import EnterWorktreeTool
from xana.tools.contracts import ToolContext, ToolInvocation


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty", "-m", "init", "-q"], check=True)


def _ctx(cwd: str, session_id: str = "s1") -> ToolContext:
    return ToolContext(session_id=session_id, agent_name="chat", cwd=cwd,
                       permission=PermissionContext(mode="default", session_id=session_id))


@pytest.mark.asyncio
async def test_spec_lists_optional_name_param() -> None:
    tool = EnterWorktreeTool(session_config_store=None,
                             worktree_root=Path("/tmp"))
    schema = tool.spec().input_schema
    assert "name" in schema["properties"]


@pytest.mark.asyncio
async def test_execute_creates_git_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt_root = tmp_path / "worktrees"
    store = FileSessionConfigStore(root=tmp_path / "cfg")
    tool = EnterWorktreeTool(session_config_store=store, worktree_root=wt_root)

    inv = ToolInvocation(tool_name="EnterWorktree", input={"name": "feat-x"},
                         call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx(cwd=str(repo)))
    assert result.ok is True, result.error
    wt = wt_root / "s1" / "feat-x"
    assert wt.exists() and (wt / ".git").exists()

    cfg = await store.get("s1")
    assert cfg.metadata.get("worktree_path") == str(wt)


@pytest.mark.asyncio
async def test_execute_rejects_unsafe_name(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    tool = EnterWorktreeTool(session_config_store=None, worktree_root=tmp_path / "wt")
    inv = ToolInvocation(tool_name="EnterWorktree", input={"name": "../escape"},
                         call_id="c1", session_id="s1")
    with pytest.raises(ValueError):
        await tool.validate(inv)


@pytest.mark.asyncio
async def test_execute_outside_git_repo_fails_cleanly(tmp_path: Path) -> None:
    tool = EnterWorktreeTool(session_config_store=None, worktree_root=tmp_path / "wt")
    inv = ToolInvocation(tool_name="EnterWorktree", input={"name": "x"},
                         call_id="c1", session_id="s1")
    result = await tool.execute(inv, _ctx(cwd=str(tmp_path)))
    assert result.ok is False
    assert "git" in (result.error or "").lower()
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_enter_worktree_tool.py -v`
Expected: fail — module missing.

- [x] **Step 3: Implement EnterWorktreeTool**

Create `src/xana/tools/builtin/enter_worktree.py`:

```python
"""EnterWorktree: create a git worktree under ~/.xana/worktrees/{sid}/{name}.

cc's analog spawns a worktree off the current branch to isolate experimental
changes. We persist the worktree path on SessionConfig.metadata so subsequent
turns can resolve cwd against it.

Rejects names containing '..' or path separators. The CALLER (model) is
expected to commit before invoking — we do NOT auto-snapshot dirty state.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.session_config.contracts import SessionConfig
from xana.tools.contracts import ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool",
        name="EnterWorktree",
        qualified_name="builtin.EnterWorktree",
        description="Create a git worktree off HEAD and switch the session into it.",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "worktree name (letters/digits/._-, max 64). Random if omitted."},
        },
    },
)


class EnterWorktreeTool(Tool):
    def __init__(self, *, session_config_store, worktree_root: Path) -> None:
        self.session_config_store = session_config_store
        self.worktree_root = Path(worktree_root)

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        name = invocation.input.get("name")
        if name is None:
            return
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"EnterWorktree: bad name {name!r}")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="worktree creation allowed")

    async def pre_tool_use(self, invocation: ToolInvocation, context: ToolContext) -> ToolInvocation:
        return invocation

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        name = invocation.input.get("name") or f"wt-{secrets.token_hex(4)}"
        target = self.worktree_root / context.session_id / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Run git worktree add HEAD off context.cwd.
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", context.cwd, "worktree", "add", str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return ToolResult(
                ok=False, content=None,
                error=stderr.decode("utf-8", errors="replace").strip() or "git worktree add failed",
                failure_kind="execution",
                rendered=f"[EnterWorktree FAILED] {stderr.decode('utf-8', errors='replace').strip()}",
            )
        if self.session_config_store is not None:
            current = await self.session_config_store.get(context.session_id)
            new_metadata = dict(current.metadata)
            new_metadata["worktree_path"] = str(target)
            await self.session_config_store.save(SessionConfig(
                session_id=current.session_id, model=current.model, effort=current.effort,
                streaming=current.streaming, thinking=current.thinking,
                search_provider=current.search_provider, project_root=current.project_root,
                metadata=new_metadata,
            ))
        return ToolResult(
            ok=True,
            content={"worktree_path": str(target)},
            rendered=f"[EnterWorktree ok] {target}",
            metadata={"worktree_path": str(target)},
        )

    async def post_tool_use(self, invocation: ToolInvocation, context: ToolContext, result: ToolResult) -> ToolResult:
        return result

    async def render_result(self, result: ToolResult) -> str:
        return result.rendered or ""

    async def cancel(self, call_id: str) -> None:
        return None
```

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_enter_worktree_tool.py -v`
Expected: 4 passed

- [x] **Step 5: Commit**

```bash
git add src/xana/tools/builtin/enter_worktree.py tests/test_enter_worktree_tool.py
git commit -m "feat(tools): EnterWorktree creates git worktree under ~/.xana/worktrees/"
```

---

### Task 5: Register all three tools in boot()

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Test: `tests/test_boot_registers_new_tools.py`

- [x] **Step 1: Write failing test**

Create `tests/test_boot_registers_new_tools.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot


@pytest.mark.asyncio
async def test_new_tools_are_registered_after_boot(tmp_path: Path) -> None:
    result = await boot(project_root=tmp_path)
    specs = await result.tool_registry.list_specs()
    names = {s.component.name for s in specs}
    assert {"TodoWrite", "EnterPlanMode", "EnterWorktree"}.issubset(names)
```

- [x] **Step 2: Run to verify fail**

Run: `pytest tests/test_boot_registers_new_tools.py -v`
Expected: fail — tools missing.

- [x] **Step 3: Wire in boot.py**

In `src/xana/runtime/boot.py`, add imports:

```python
from xana.tools.builtin._todo_store import TodoStore
from xana.tools.builtin.todo_write import TodoWriteTool
from xana.tools.builtin.enter_plan_mode import EnterPlanModeTool
from xana.tools.builtin.enter_worktree import EnterWorktreeTool
```

After the existing `await tools.register(BashTool())` line, add:

```python
todo_root = Path.home() / ".xana" / "sessions"
worktree_root = Path.home() / ".xana" / "worktrees"
await tools.register(TodoWriteTool(TodoStore(root=todo_root)))
await tools.register(EnterPlanModeTool(session_config_store=session_config_store))
await tools.register(EnterWorktreeTool(session_config_store=session_config_store, worktree_root=worktree_root))
```

Note: this assumes Plan 1's `session_config_store` is wired. If not, pass `None` for `session_config_store`.

- [x] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_boot_registers_new_tools.py -v && pytest -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add src/xana/runtime/boot.py tests/test_boot_registers_new_tools.py
git commit -m "feat(boot): register TodoWrite, EnterPlanMode, EnterWorktree tools"
```

---

### Task 6: Smoke test end-to-end via fake provider

**Files:**
- Test: `tests/test_new_tools_end_to_end.py`

- [x] **Step 1: Write a fake-provider round-trip test**

Create `tests/test_new_tools_end_to_end.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot
from xana.tools.builtin._todo_store import TodoStore
from xana.tools.contracts import ToolContext, ToolInvocation
from xana.policy.contracts import PermissionContext


@pytest.mark.asyncio
async def test_todo_write_persists_and_reloads(tmp_path: Path) -> None:
    """Boot, invoke TodoWriteTool directly via registry, check the JSON sidecar."""
    result = await boot(project_root=tmp_path)
    tool = await result.tool_registry.get("TodoWrite")
    assert tool is not None
    inv = ToolInvocation(
        tool_name="TodoWrite",
        input={"todos": [{"content": "ship", "active_form": "Shipping", "status": "in_progress"}]},
        call_id="c1", session_id="boot-smoke",
    )
    ctx = ToolContext(session_id="boot-smoke", agent_name="chat", cwd=str(tmp_path),
                      permission=PermissionContext(mode="default", session_id="boot-smoke"))
    res = await tool.execute(inv, ctx)
    assert res.ok is True
    store = TodoStore(root=Path.home() / ".xana" / "sessions")
    loaded = store.load("boot-smoke")
    assert len(loaded) == 1 and loaded[0].content == "ship"
    store.save("boot-smoke", ())  # cleanup
```

- [x] **Step 2: Run**

Run: `pytest tests/test_new_tools_end_to_end.py -v`
Expected: pass

- [x] **Step 3: Commit**

```bash
git add tests/test_new_tools_end_to_end.py
git commit -m "test: end-to-end TodoWrite via boot+registry"
```

---

### Task 7: Regression sweep

- [x] **Step 1: Full pytest**

Run: `pytest -q`
Expected: 0 failures.

- [x] **Step 2: Verify tool schemas reach the model**

In a fresh REPL (`xanac --session tool-smoke`), with real DS, ask:
> "list your available tools"

Expected: model output mentions TodoWrite, EnterPlanMode, EnterWorktree alongside the existing ones (Read, Edit, Write, Bash, Glob, Grep, memory_search).

- [x] **Step 3: Verify TodoWrite end-to-end via model**

> "Make a todo list with 3 items about reviewing this codebase"

Expected: model invokes TodoWrite; the rendered footer shows `[TodoWrite ok] 3 item(s)`; `cat ~/.xana/sessions/tool-smoke.todos.json` shows the items.

---

**End of plan.** Total expected commits: ~7.
