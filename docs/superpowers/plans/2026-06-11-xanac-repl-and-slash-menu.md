# xanac Interactive REPL + Slash Command Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn xanac from a one-shot CLI into an interactive REPL with 11 slash commands and Ctrl-C cancel, backed by a new `SessionConfigStore` interface for persisting `/model`, `/effort`, `/streaming`, `/thinking`, `/search_provider` selections across REPL turns and sessions.

**Architecture:**
- Add `prompt_toolkit` dep. `xanac` (no positional text) drops into REPL mode; positional text keeps the existing one-shot path.
- A new `xana/session_config/{interfaces,contracts}.py` defines `SessionConfigStore` + `SessionConfig` dataclass. `FileSessionConfigStore` persists JSON under `~/.xana/sessions/{session_id}.json`. CLI reads the store before each `submit()` and threads the values into `HarnessContext` via the existing `model`, `effort`, and `config_snapshot` fields — zero changes to `HarnessContext` itself.
- A new `DefaultCommandRouter` (in `agent_adapter/router.py` alongside the existing request router) implements all 11 commands. `DefaultAgentGateway.submit()` is taught to call `command_router.route_command()` first; only non-command inbound falls through to the agent.
- Ctrl-C while a turn is in flight calls `gateway.cancel(session_id, run_id)`. The REPL tracks the in-flight task and cancels it via `asyncio.Task.cancel()`. The harness's existing `AbortSignal` mechanism is extended in `DefaultHarness._invoke` to honor a session-scoped cancel flag the gateway sets.

**Tech Stack:** Python 3.10+, prompt_toolkit ≥ 3.0, pytest-asyncio (already present), existing xana interfaces.

**File structure:**
- Create: `src/xana/session_config/__init__.py`
- Create: `src/xana/session_config/contracts.py` — `SessionConfig` dataclass
- Create: `src/xana/session_config/interfaces.py` — `SessionConfigStore` ABC
- Create: `src/xana/session_config/file_store.py` — `FileSessionConfigStore`
- Create: `src/xana/agent_adapter/commands.py` — `DefaultCommandRouter` + 11 handlers
- Modify: `src/xana/agent_adapter/gateway.py` — dispatch command_router before agent
- Modify: `src/xana/cli/xanac.py` — `--interactive` flag, REPL loop
- Modify: `src/xana/runtime/boot.py` — instantiate FileSessionConfigStore, expose on BootResult
- Modify: `src/xana/agents/chat_agent.py` — read SessionConfig and forward to HarnessContext
- Modify: `pyproject.toml` — add `prompt_toolkit>=3.0`
- Test: `tests/test_session_config_store.py`
- Test: `tests/test_default_command_router.py`
- Test: `tests/test_gateway_command_dispatch.py`
- Test: `tests/test_xanac_repl_loop.py`

---

### Task 1: Add prompt_toolkit dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit pyproject.toml dependencies**

Open `pyproject.toml` and change the `dependencies` array to include `prompt_toolkit`:

```toml
dependencies = [
    "pydantic>=2",
    "httpx>=0.28",
    "pyyaml>=6",
    "prompt_toolkit>=3.0",
]
```

- [ ] **Step 2: Install editable**

Run: `pip install -e .`
Expected: `Successfully installed ... prompt_toolkit-3.x.x ...`

- [ ] **Step 3: Smoke import**

Run: `python -c "from prompt_toolkit import PromptSession; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): add prompt_toolkit for xanac REPL"
```

---

### Task 2: SessionConfig contract

**Files:**
- Create: `src/xana/session_config/__init__.py`
- Create: `src/xana/session_config/contracts.py`
- Test: `tests/test_session_config_contracts.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_config_contracts.py`:

```python
from __future__ import annotations

from xana.session_config.contracts import SessionConfig


def test_session_config_default_field_values() -> None:
    cfg = SessionConfig(session_id="s1")
    assert cfg.session_id == "s1"
    assert cfg.model is None
    assert cfg.effort is None
    assert cfg.streaming is True
    assert cfg.thinking is False
    assert cfg.search_provider is None
    assert cfg.project_root is None
    assert cfg.metadata == {}


def test_session_config_is_frozen() -> None:
    cfg = SessionConfig(session_id="s1")
    import dataclasses
    assert dataclasses.is_dataclass(cfg)
    try:
        cfg.model = "x"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("SessionConfig must be frozen")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_config_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xana.session_config'`

- [ ] **Step 3: Create the package init file**

Create `src/xana/session_config/__init__.py` with content:

```python
"""Session-scoped configuration store (model, effort, streaming, thinking, etc.)."""
```

- [ ] **Step 4: Create the SessionConfig dataclass**

Create `src/xana/session_config/contracts.py`:

```python
"""SessionConfig: per-session runtime knobs the user toggles via /model, /effort, etc.

This is intentionally small and frozen. The CLI persists it via SessionConfigStore
and re-reads before each turn — there is no mid-turn mutation. Fields that map
1:1 onto HarnessContext (model, effort) are threaded through ChatAgent; the rest
live in HarnessContext.config_snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    model: str | None = None
    effort: str | int | None = None
    streaming: bool = True
    thinking: bool = False
    search_provider: str | None = None
    project_root: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_session_config_contracts.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/xana/session_config/__init__.py src/xana/session_config/contracts.py tests/test_session_config_contracts.py
git commit -m "feat(session_config): SessionConfig dataclass for runtime knobs"
```

---

### Task 3: SessionConfigStore interface

**Files:**
- Create: `src/xana/session_config/interfaces.py`
- Test: `tests/test_session_config_interface.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_config_interface.py`:

```python
from __future__ import annotations

import inspect

from xana.session_config.interfaces import SessionConfigStore


def test_session_config_store_is_abc_with_expected_methods() -> None:
    methods = {name for name, _ in inspect.getmembers(SessionConfigStore, inspect.isfunction)}
    # Must define: get, save, delete
    assert {"get", "save", "delete"}.issubset(methods)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_config_interface.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create the SessionConfigStore ABC**

Create `src/xana/session_config/interfaces.py`:

```python
"""SessionConfigStore: persistence boundary for per-session runtime knobs.

Read by the CLI/REPL before each turn; written by slash-command handlers that
mutate config (/model, /effort, /streaming, /thinking, /search_provider).

Implementations must be safe to call concurrently from multiple REPL processes
sharing the same session_id (treat the storage path as authoritative — re-read
on every get).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.session_config.contracts import SessionConfig


class SessionConfigStore(ABC):
    @abstractmethod
    async def get(self, session_id: str) -> SessionConfig: ...

    @abstractmethod
    async def save(self, config: SessionConfig) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_session_config_interface.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add src/xana/session_config/interfaces.py tests/test_session_config_interface.py
git commit -m "feat(session_config): SessionConfigStore abstract interface"
```

---

### Task 4: FileSessionConfigStore implementation

**Files:**
- Create: `src/xana/session_config/file_store.py`
- Test: `tests/test_file_session_config_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_file_session_config_store.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.session_config.contracts import SessionConfig
from xana.session_config.file_store import FileSessionConfigStore


@pytest.mark.asyncio
async def test_get_unknown_session_returns_defaults(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    cfg = await store.get("nope")
    assert cfg.session_id == "nope"
    assert cfg.model is None
    assert cfg.streaming is True


@pytest.mark.asyncio
async def test_save_then_get_roundtrip(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    saved = SessionConfig(
        session_id="s1",
        model="deepseek-v4-flash",
        effort="medium",
        streaming=False,
        thinking=True,
        search_provider="bm25",
    )
    await store.save(saved)
    loaded = await store.get("s1")
    assert loaded == saved


@pytest.mark.asyncio
async def test_save_writes_atomic_file_at_expected_path(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    await store.save(SessionConfig(session_id="s1", model="x"))
    f = tmp_path / "s1.json"
    assert f.exists()
    assert "deepseek" not in f.read_text()  # we wrote model="x"
    assert '"model": "x"' in f.read_text()


@pytest.mark.asyncio
async def test_delete_removes_file(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    await store.save(SessionConfig(session_id="s1", model="x"))
    await store.delete("s1")
    assert not (tmp_path / "s1.json").exists()
    # Subsequent get returns defaults — no crash.
    cfg = await store.get("s1")
    assert cfg.model is None


@pytest.mark.asyncio
async def test_get_path_traversal_rejected(tmp_path: Path) -> None:
    store = FileSessionConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        await store.get("../etc/passwd")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_file_session_config_store.py -v`
Expected: FAIL — `ModuleNotFoundError: file_store`

- [ ] **Step 3: Implement FileSessionConfigStore**

Create `src/xana/session_config/file_store.py`:

```python
"""FileSessionConfigStore: JSON-per-session under ~/.xana/sessions/.

Atomic write via temp file + rename. Rejects session_ids containing path
separators or `..` to prevent traversal — slash-command handlers feed
user-controlled strings in, so the guard is load-bearing.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from xana.session_config.contracts import SessionConfig
from xana.session_config.interfaces import SessionConfigStore


class FileSessionConfigStore(SessionConfigStore):
    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id.split("/"):
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self.root / f"{session_id}.json"

    async def get(self, session_id: str) -> SessionConfig:
        path = self._path(session_id)
        if not path.exists():
            return SessionConfig(session_id=session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session_id"] = session_id
        return SessionConfig(**data)

    async def save(self, config: SessionConfig) -> None:
        path = self._path(config.session_id)
        tmp = path.with_suffix(".json.tmp")
        payload = dataclasses.asdict(config)
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_file_session_config_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/xana/session_config/file_store.py tests/test_file_session_config_store.py
git commit -m "feat(session_config): FileSessionConfigStore with atomic JSON writes"
```

---

### Task 5: Wire SessionConfigStore into BootResult

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Test: `tests/test_boot_session_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_boot_session_config.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot
from xana.session_config.interfaces import SessionConfigStore


@pytest.mark.asyncio
async def test_boot_exposes_session_config_store(tmp_path: Path) -> None:
    result = await boot(project_root=tmp_path)
    assert isinstance(result.session_config_store, SessionConfigStore)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_boot_session_config.py -v`
Expected: FAIL — `BootResult has no attribute 'session_config_store'`

- [ ] **Step 3: Add session_config_store to BootResult**

In `src/xana/runtime/boot.py`, add the import near the other interface imports:

```python
from xana.session_config.file_store import FileSessionConfigStore
from xana.session_config.interfaces import SessionConfigStore
```

Add `session_config_store: SessionConfigStore` to the `BootResult` dataclass (insert after `state_repository: StateRepository`).

In the `boot()` function body, before constructing `BootResult`, insert:

```python
session_config_root = Path.home() / ".xana" / "sessions"
session_config_store: SessionConfigStore = FileSessionConfigStore(root=session_config_root)
```

Add `session_config_store=session_config_store,` to the `BootResult(...)` constructor call.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_boot_session_config.py -v && pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/runtime/boot.py tests/test_boot_session_config.py
git commit -m "feat(boot): expose FileSessionConfigStore on BootResult"
```

---

### Task 6: DefaultCommandRouter with /help registration

**Files:**
- Create: `src/xana/agent_adapter/commands.py`
- Test: `tests/test_default_command_router.py`

- [ ] **Step 1: Write the failing test (router skeleton + /help)**

Create `tests/test_default_command_router.py`:

```python
from __future__ import annotations

import pytest

from xana.agent_adapter.commands import DefaultCommandRouter
from xana.agent_adapter.contracts import InboundMessage


@pytest.mark.asyncio
async def test_router_lists_all_eleven_default_commands() -> None:
    router = DefaultCommandRouter()
    specs = await router.list_commands()
    names = {s.name for s in specs}
    assert names == {
        "/help", "/clear", "/compact", "/model", "/effort",
        "/streaming", "/thinking", "/attach", "/cancel",
        "/project", "/search_provider",
    }


@pytest.mark.asyncio
async def test_help_with_no_args_lists_all_commands() -> None:
    router = DefaultCommandRouter()
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/help")
    )
    assert result.ok is True
    for name in ["/help", "/clear", "/model", "/effort"]:
        assert name in result.message


@pytest.mark.asyncio
async def test_help_with_known_command_shows_description() -> None:
    router = DefaultCommandRouter()
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/help /model")
    )
    assert result.ok is True
    assert "model" in result.message.lower()


@pytest.mark.asyncio
async def test_non_command_text_returns_ok_false_with_pass_through_marker() -> None:
    router = DefaultCommandRouter()
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="hello world")
    )
    assert result.ok is False
    assert result.payload.get("pass_through") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_default_command_router.py -v`
Expected: FAIL — `ModuleNotFoundError: commands`

- [ ] **Step 3: Create the DefaultCommandRouter skeleton + /help**

Create `src/xana/agent_adapter/commands.py`:

```python
"""DefaultCommandRouter: routes 11 built-in slash commands.

Commands that mutate persistent state delegate to SessionConfigStore;
commands that mutate ephemeral state (history clear/compact, cancel current
turn) talk to ActiveContextStore and AgentGateway directly. /help is
self-contained — only the router knows the full command list.

Adapters must:
  1. call route_command() on every inbound;
  2. if CommandResult.ok is False AND payload['pass_through'] is True, treat
     the inbound as plain user text and forward to the agent;
  3. otherwise consume CommandResult.message + payload.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from xana.agent_adapter.contracts import CommandResult, CommandSpec, InboundMessage
from xana.agent_adapter.interfaces import CommandRouter


_HandlerFn = Callable[["DefaultCommandRouter", InboundMessage, str], Awaitable[CommandResult]]


class DefaultCommandRouter(CommandRouter):
    def __init__(
        self,
        *,
        session_config_store: Any = None,  # SessionConfigStore | None — typed loose to avoid cycles in tests
        active_context_store: Any = None,
        gateway: Any = None,
        available_models: tuple[str, ...] = (),
        available_search_providers: tuple[str, ...] = (),
    ) -> None:
        self.session_config_store = session_config_store
        self.active_context_store = active_context_store
        self.gateway = gateway
        self.available_models = available_models
        self.available_search_providers = available_search_providers

        self._specs: tuple[CommandSpec, ...] = (
            CommandSpec(name="/help",     description="显示命令菜单。用法 `/help` 或 `/help /命令`"),
            CommandSpec(name="/clear",    description="清空当前会话上下文"),
            CommandSpec(name="/compact",  description="压缩本会话历史成一条 summary"),
            CommandSpec(name="/model",    description="切换本会话模型，无参列出可选"),
            CommandSpec(name="/effort",   description="推理强度 low/medium/high"),
            CommandSpec(name="/streaming", description="token 流开关 on/off"),
            CommandSpec(name="/thinking", description="思考链开关 on/off"),
            CommandSpec(name="/attach",   description="切换 task 订阅"),
            CommandSpec(name="/cancel",   description="取消当前 turn"),
            CommandSpec(name="/project",  description="项目管理"),
            CommandSpec(name="/search_provider", description="搜索引擎切换"),
        )
        self._handlers: dict[str, _HandlerFn] = {
            "/help": DefaultCommandRouter._cmd_help,
        }

    async def list_commands(self) -> tuple[CommandSpec, ...]:
        return self._specs

    async def route_command(self, inbound: InboundMessage) -> CommandResult:
        text = inbound.text.strip()
        if not text.startswith("/"):
            return CommandResult(ok=False, message="", payload={"pass_through": True})
        parts = text.split(maxsplit=1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        handler = self._handlers.get(name)
        if handler is None:
            return CommandResult(
                ok=False, message=f"未注册的命令 {name}",
                payload={"pass_through": False, "unknown_command": True},
            )
        return await handler(self, inbound, args)

    async def _cmd_help(self, inbound: InboundMessage, args: str) -> CommandResult:
        args = args.strip()
        if args:
            target = args.split()[0]
            for spec in self._specs:
                if spec.name == target:
                    return CommandResult(ok=True, message=f"{spec.name} — {spec.description}")
            return CommandResult(ok=False, message=f"未知命令 {target}")
        lines = ["可用命令:"]
        for spec in self._specs:
            lines.append(f"  {spec.name:<20} {spec.description}")
        return CommandResult(ok=True, message="\n".join(lines))
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_default_command_router.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/commands.py tests/test_default_command_router.py
git commit -m "feat(commands): DefaultCommandRouter with /help + 11-command spec list"
```

---

### Task 7: /clear and /compact handlers

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`
- Modify: `tests/test_default_command_router.py`

- [ ] **Step 1: Add failing tests for /clear and /compact**

Append to `tests/test_default_command_router.py`:

```python
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_clear_calls_active_context_store_clear() -> None:
    store = AsyncMock()
    router = DefaultCommandRouter(active_context_store=store)
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/clear")
    )
    assert result.ok is True
    store.clear.assert_awaited_once_with("s1", "chat")


@pytest.mark.asyncio
async def test_clear_without_store_returns_error() -> None:
    router = DefaultCommandRouter()
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/clear")
    )
    assert result.ok is False
    assert "store" in result.message.lower()


@pytest.mark.asyncio
async def test_compact_passes_user_summary_to_store() -> None:
    store = AsyncMock()
    router = DefaultCommandRouter(active_context_store=store)
    result = await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/compact talked about X")
    )
    assert result.ok is True
    store.compact.assert_awaited_once_with("s1", "chat", "talked about X")


@pytest.mark.asyncio
async def test_compact_with_no_args_uses_generic_summary() -> None:
    store = AsyncMock()
    router = DefaultCommandRouter(active_context_store=store)
    await router.route_command(
        InboundMessage(adapter_id="cli", session_id="s1", text="/compact")
    )
    store.compact.assert_awaited_once()
    args, _ = store.compact.call_args
    assert args[2] != ""  # some non-empty summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_default_command_router.py -v -k "clear or compact"`
Expected: 4 fail — handlers not registered

- [ ] **Step 3: Implement handlers**

In `src/xana/agent_adapter/commands.py`, replace the `_handlers` dict in `__init__` with:

```python
self._handlers: dict[str, _HandlerFn] = {
    "/help":     DefaultCommandRouter._cmd_help,
    "/clear":    DefaultCommandRouter._cmd_clear,
    "/compact":  DefaultCommandRouter._cmd_compact,
}
```

Append these methods to `DefaultCommandRouter`:

```python
async def _cmd_clear(self, inbound: InboundMessage, args: str) -> CommandResult:
    if self.active_context_store is None:
        return CommandResult(ok=False, message="active_context_store 未配置")
    await self.active_context_store.clear(inbound.session_id, "chat")
    return CommandResult(ok=True, message="[cleared] 上下文已清空")

async def _cmd_compact(self, inbound: InboundMessage, args: str) -> CommandResult:
    if self.active_context_store is None:
        return CommandResult(ok=False, message="active_context_store 未配置")
    summary = args.strip() or "(用户触发的压缩 — 无具体摘要)"
    await self.active_context_store.compact(inbound.session_id, "chat", summary)
    return CommandResult(ok=True, message="[compacted] 历史已压缩")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_default_command_router.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/commands.py tests/test_default_command_router.py
git commit -m "feat(commands): /clear and /compact wire to ActiveContextStore"
```

---

### Task 8: /model, /effort, /streaming, /thinking, /search_provider handlers

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`
- Modify: `tests/test_default_command_router.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_default_command_router.py`:

```python
from xana.session_config.contracts import SessionConfig


class _FakeStore:
    def __init__(self) -> None:
        self.saved: SessionConfig | None = None

    async def get(self, sid: str) -> SessionConfig:
        return self.saved or SessionConfig(session_id=sid)

    async def save(self, cfg: SessionConfig) -> None:
        self.saved = cfg

    async def delete(self, sid: str) -> None:
        self.saved = None


@pytest.mark.asyncio
async def test_model_with_no_args_lists_available() -> None:
    router = DefaultCommandRouter(
        session_config_store=_FakeStore(),
        available_models=("deepseek-v4-flash", "deepseek-v4"),
    )
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/model"))
    assert r.ok is True
    assert "deepseek-v4-flash" in r.message and "deepseek-v4" in r.message


@pytest.mark.asyncio
async def test_model_with_known_arg_persists_choice() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(
        session_config_store=store,
        available_models=("deepseek-v4-flash", "deepseek-v4"),
    )
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/model deepseek-v4"))
    assert r.ok is True
    assert store.saved is not None and store.saved.model == "deepseek-v4"


@pytest.mark.asyncio
async def test_model_with_unknown_arg_rejected() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(
        session_config_store=store,
        available_models=("deepseek-v4-flash",),
    )
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/model nope"))
    assert r.ok is False
    assert store.saved is None


@pytest.mark.asyncio
async def test_effort_persists_low_medium_high() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/effort medium"))
    assert r.ok is True and store.saved.effort == "medium"


@pytest.mark.asyncio
async def test_effort_rejects_bad_value() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/effort yolo"))
    assert r.ok is False and store.saved is None


@pytest.mark.asyncio
async def test_streaming_toggle_on_off() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/streaming off"))
    assert store.saved.streaming is False
    await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/streaming on"))
    assert store.saved.streaming is True


@pytest.mark.asyncio
async def test_thinking_toggle_on_off() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/thinking on"))
    assert store.saved.thinking is True


@pytest.mark.asyncio
async def test_search_provider_picks_from_available() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(
        session_config_store=store,
        available_search_providers=("bm25", "embedding"),
    )
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/search_provider embedding"))
    assert r.ok is True and store.saved.search_provider == "embedding"
```

- [ ] **Step 2: Run tests to verify fail**

Run: `pytest tests/test_default_command_router.py -v -k "model or effort or streaming or thinking or search_provider"`
Expected: ~8 fail

- [ ] **Step 3: Implement handlers**

Add to the `_handlers` dict in `DefaultCommandRouter.__init__`:

```python
"/model":           DefaultCommandRouter._cmd_model,
"/effort":          DefaultCommandRouter._cmd_effort,
"/streaming":       DefaultCommandRouter._cmd_streaming,
"/thinking":        DefaultCommandRouter._cmd_thinking,
"/search_provider": DefaultCommandRouter._cmd_search_provider,
```

Append to the class:

```python
async def _persist_with(self, session_id: str, **changes) -> "SessionConfig":
    from xana.session_config.contracts import SessionConfig
    if self.session_config_store is None:
        raise RuntimeError("session_config_store 未配置")
    current = await self.session_config_store.get(session_id)
    new = SessionConfig(
        session_id=current.session_id,
        model=changes.get("model", current.model),
        effort=changes.get("effort", current.effort),
        streaming=changes.get("streaming", current.streaming),
        thinking=changes.get("thinking", current.thinking),
        search_provider=changes.get("search_provider", current.search_provider),
        project_root=changes.get("project_root", current.project_root),
        metadata=current.metadata,
    )
    await self.session_config_store.save(new)
    return new

async def _cmd_model(self, inbound: InboundMessage, args: str) -> CommandResult:
    target = args.strip()
    if not target:
        if not self.available_models:
            return CommandResult(ok=True, message="未配置可选模型")
        return CommandResult(ok=True, message="可选模型: " + ", ".join(self.available_models))
    if self.available_models and target not in self.available_models:
        return CommandResult(ok=False, message=f"未知模型 {target}; 可选: " + ", ".join(self.available_models))
    new = await self._persist_with(inbound.session_id, model=target)
    return CommandResult(ok=True, message=f"模型切换为 {new.model}")

async def _cmd_effort(self, inbound: InboundMessage, args: str) -> CommandResult:
    target = args.strip().lower()
    if target not in {"low", "medium", "high", "max"}:
        return CommandResult(ok=False, message="effort 必须为 low/medium/high/max")
    new = await self._persist_with(inbound.session_id, effort=target)
    return CommandResult(ok=True, message=f"effort 切换为 {new.effort}")

async def _cmd_streaming(self, inbound: InboundMessage, args: str) -> CommandResult:
    val = args.strip().lower()
    if val not in {"on", "off"}:
        return CommandResult(ok=False, message="streaming 必须为 on/off")
    new = await self._persist_with(inbound.session_id, streaming=(val == "on"))
    return CommandResult(ok=True, message=f"streaming={new.streaming}")

async def _cmd_thinking(self, inbound: InboundMessage, args: str) -> CommandResult:
    val = args.strip().lower()
    if val not in {"on", "off"}:
        return CommandResult(ok=False, message="thinking 必须为 on/off")
    new = await self._persist_with(inbound.session_id, thinking=(val == "on"))
    return CommandResult(ok=True, message=f"thinking={new.thinking}")

async def _cmd_search_provider(self, inbound: InboundMessage, args: str) -> CommandResult:
    target = args.strip()
    if not target:
        if not self.available_search_providers:
            return CommandResult(ok=True, message="未配置可选搜索引擎")
        return CommandResult(ok=True, message="可选搜索引擎: " + ", ".join(self.available_search_providers))
    if self.available_search_providers and target not in self.available_search_providers:
        return CommandResult(ok=False, message=f"未知搜索引擎 {target}")
    new = await self._persist_with(inbound.session_id, search_provider=target)
    return CommandResult(ok=True, message=f"搜索引擎切换为 {new.search_provider}")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_default_command_router.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/commands.py tests/test_default_command_router.py
git commit -m "feat(commands): /model /effort /streaming /thinking /search_provider via SessionConfigStore"
```

---

### Task 9: /cancel, /attach, /project handlers

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`
- Modify: `tests/test_default_command_router.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_default_command_router.py`:

```python
@pytest.mark.asyncio
async def test_cancel_calls_gateway_cancel() -> None:
    gateway = AsyncMock()
    router = DefaultCommandRouter(gateway=gateway)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/cancel"))
    assert r.ok is True
    gateway.cancel.assert_awaited_once_with("s1", None)


@pytest.mark.asyncio
async def test_attach_persists_attach_marker() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/attach"))
    assert r.ok is True
    # /attach is a UX toggle; we persist it in metadata so the REPL can read it back.
    assert store.saved.metadata.get("attached") is True


@pytest.mark.asyncio
async def test_project_with_arg_sets_project_root() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/project /tmp/foo"))
    assert r.ok is True
    assert store.saved.project_root == "/tmp/foo"


@pytest.mark.asyncio
async def test_project_with_no_arg_shows_current() -> None:
    store = _FakeStore()
    router = DefaultCommandRouter(session_config_store=store)
    r = await router.route_command(InboundMessage(adapter_id="cli", session_id="s1", text="/project"))
    assert r.ok is True
    assert "project" in r.message.lower()
```

- [ ] **Step 2: Run tests to verify fail**

Run: `pytest tests/test_default_command_router.py -v -k "cancel or attach or project"`
Expected: 4 fail

- [ ] **Step 3: Implement handlers**

Add to the `_handlers` dict:

```python
"/cancel":   DefaultCommandRouter._cmd_cancel,
"/attach":   DefaultCommandRouter._cmd_attach,
"/project":  DefaultCommandRouter._cmd_project,
```

Append to the class:

```python
async def _cmd_cancel(self, inbound: InboundMessage, args: str) -> CommandResult:
    if self.gateway is None:
        return CommandResult(ok=False, message="gateway 未注入")
    await self.gateway.cancel(inbound.session_id, None)
    return CommandResult(ok=True, message="[cancel] 已请求取消当前 turn")

async def _cmd_attach(self, inbound: InboundMessage, args: str) -> CommandResult:
    if self.session_config_store is None:
        return CommandResult(ok=False, message="session_config_store 未配置")
    current = await self.session_config_store.get(inbound.session_id)
    next_attached = not bool(current.metadata.get("attached"))
    from xana.session_config.contracts import SessionConfig
    new_metadata = dict(current.metadata)
    new_metadata["attached"] = next_attached
    new = SessionConfig(
        session_id=current.session_id, model=current.model, effort=current.effort,
        streaming=current.streaming, thinking=current.thinking,
        search_provider=current.search_provider, project_root=current.project_root,
        metadata=new_metadata,
    )
    await self.session_config_store.save(new)
    return CommandResult(ok=True, message=f"attached={next_attached}")

async def _cmd_project(self, inbound: InboundMessage, args: str) -> CommandResult:
    target = args.strip()
    if not target:
        if self.session_config_store is None:
            return CommandResult(ok=True, message="未配置 project_root")
        cur = await self.session_config_store.get(inbound.session_id)
        return CommandResult(ok=True, message=f"当前 project_root: {cur.project_root or '(未设置)'}")
    if self.session_config_store is None:
        return CommandResult(ok=False, message="session_config_store 未配置")
    new = await self._persist_with(inbound.session_id, project_root=target)
    return CommandResult(ok=True, message=f"project_root 切换为 {new.project_root}")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_default_command_router.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/commands.py tests/test_default_command_router.py
git commit -m "feat(commands): /cancel /attach /project handlers"
```

---

### Task 10: Wire CommandRouter into DefaultAgentGateway.submit()

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py`
- Test: `tests/test_gateway_command_dispatch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_gateway_command_dispatch.py`:

```python
from __future__ import annotations

from typing import AsyncIterator

import pytest

from xana.agent_adapter.contracts import (
    AdapterHealth, CommandResult, CommandSpec, InboundMessage, RenderedMessage, SessionBinding,
)
from xana.agent_adapter.gateway import DefaultAgentGateway
from xana.agent_adapter.interfaces import Adapter, AgentRequestRouter, CommandRouter
from xana.agents.contracts import AgentEvent, AgentRequest
from xana.agents.interfaces import Agent


class _RouterStub(AgentRequestRouter):
    async def route(self, inbound: InboundMessage) -> AgentRequest:
        return AgentRequest(session_id=inbound.session_id, text=inbound.text,
                            source=inbound.adapter_id, target_agent="chat", attachments=(), metadata={})


class _AgentStub(Agent):
    def spec(self): ...
    async def cancel(self, session_id, run_id=None): ...
    async def receive_inbound(self, inbound): ...
    def send(self, request):
        async def gen():
            yield AgentEvent(kind="final", session_id=request.session_id, payload={"text": "agent-handled"})
        return gen()


class _AdapterStub(Adapter):
    def __init__(self) -> None:
        self.rendered: list[AgentEvent] = []
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def receive(self) -> AsyncIterator[InboundMessage]:
        raise NotImplementedError
    async def render(self, event: AgentEvent) -> RenderedMessage:
        self.rendered.append(event)
        return RenderedMessage(adapter_id="cli", session_id=event.session_id,
                               text=str(event.payload.get("text", "")))
    async def bind_session(self, binding: SessionBinding) -> None: ...
    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id="cli", status="healthy")


class _CmdRouter(CommandRouter):
    def __init__(self, *, intercept: bool) -> None:
        self.intercept = intercept
        self.calls: list[InboundMessage] = []
    async def list_commands(self):
        return (CommandSpec(name="/help", description=""),)
    async def route_command(self, inbound):
        self.calls.append(inbound)
        if self.intercept:
            return CommandResult(ok=True, message="cmd-handled", payload={})
        return CommandResult(ok=False, message="", payload={"pass_through": True})


@pytest.mark.asyncio
async def test_intercepted_command_short_circuits_agent() -> None:
    adapter = _AdapterStub()
    gw = DefaultAgentGateway(agents={"chat": _AgentStub()}, router=_RouterStub())
    await gw.register_adapter("cli", adapter)
    await gw.register_command_router(_CmdRouter(intercept=True))
    out = []
    async for r in gw.submit(InboundMessage(adapter_id="cli", session_id="s1", text="/help")):
        out.append(r)
    assert any("cmd-handled" in r.text for r in out)
    assert all("agent-handled" not in r.text for r in out)


@pytest.mark.asyncio
async def test_pass_through_command_falls_to_agent() -> None:
    adapter = _AdapterStub()
    gw = DefaultAgentGateway(agents={"chat": _AgentStub()}, router=_RouterStub())
    await gw.register_adapter("cli", adapter)
    await gw.register_command_router(_CmdRouter(intercept=False))
    out = []
    async for r in gw.submit(InboundMessage(adapter_id="cli", session_id="s1", text="hello")):
        out.append(r)
    assert any("agent-handled" in r.text for r in out)


@pytest.mark.asyncio
async def test_no_command_router_falls_to_agent() -> None:
    adapter = _AdapterStub()
    gw = DefaultAgentGateway(agents={"chat": _AgentStub()}, router=_RouterStub())
    await gw.register_adapter("cli", adapter)
    out = []
    async for r in gw.submit(InboundMessage(adapter_id="cli", session_id="s1", text="hello")):
        out.append(r)
    assert any("agent-handled" in r.text for r in out)
```

- [ ] **Step 2: Run tests to verify fail**

Run: `pytest tests/test_gateway_command_dispatch.py -v`
Expected: tests for command-intercept will pass-through to agent (wrong) and fail.

- [ ] **Step 3: Modify DefaultAgentGateway._submit**

In `src/xana/agent_adapter/gateway.py`, replace `_submit` with:

```python
async def _submit(self, inbound: InboundMessage) -> AsyncGenerator[RenderedMessage, None]:
    adapter = self.adapters.get(inbound.adapter_id)
    if adapter is None:
        yield RenderedMessage(
            adapter_id=inbound.adapter_id, session_id=inbound.session_id,
            text=f"[gateway] unknown adapter={inbound.adapter_id!r}",
            metadata={"event_kind": "error"},
        )
        return

    # Slash-command short-circuit: ask the command_router first. If it intercepts
    # (ok=True or ok=False with pass_through=False), render the result and return.
    if self.command_router is not None:
        cmd_result = await self.command_router.route_command(inbound)
        if cmd_result.ok or not cmd_result.payload.get("pass_through", False):
            event = AgentEvent(
                kind="final" if cmd_result.ok else "error",
                session_id=inbound.session_id,
                payload={"text": cmd_result.message, **cmd_result.payload},
            )
            rendered = await adapter.render(event)
            yield self._attach_event_kind(rendered, event)
            return

    request = await self.router.route(inbound)
    target = request.target_agent or ""
    agent = self.agents.get(target)
    if agent is None:
        yield RenderedMessage(
            adapter_id=inbound.adapter_id, session_id=inbound.session_id,
            text=f"[gateway] unknown agent={target!r}",
            metadata={"event_kind": "error"},
        )
        return
    async for event in agent.send(request):
        rendered = await adapter.render(event)
        yield self._attach_event_kind(rendered, event)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_gateway_command_dispatch.py -v && pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/agent_adapter/gateway.py tests/test_gateway_command_dispatch.py
git commit -m "feat(gateway): dispatch through CommandRouter before forwarding to agent"
```

---

### Task 11: Wire CommandRouter into boot + xanac

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Modify: `src/xana/cli/xanac.py`
- Test: `tests/test_xanac_one_shot_command.py`

- [ ] **Step 1: Write failing one-shot command test**

Create `tests/test_xanac_one_shot_command.py`:

```python
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from xana.cli.xanac import _run
import argparse


@pytest.mark.asyncio
async def test_one_shot_help_runs_command_router_not_agent(tmp_path: Path, capsys) -> None:
    args = argparse.Namespace(text=["/help"], project=tmp_path, session="s1", interactive=False)
    rc = await _run(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "/help" in captured.out and "/model" in captured.out
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_xanac_one_shot_command.py -v`
Expected: fail — `interactive` arg unknown, or command_router not wired in boot.

- [ ] **Step 3: Wire command_router into boot**

In `src/xana/runtime/boot.py`, add the import:

```python
from xana.agent_adapter.commands import DefaultCommandRouter
```

After constructing `gateway`, before `return BootResult(...)`:

```python
command_router = DefaultCommandRouter(
    session_config_store=session_config_store,
    active_context_store=active_ctx,
    gateway=gateway,
    available_models=tuple(runtime_config.models.keys()) if hasattr(runtime_config, "models") else (model,),
    available_search_providers=("bm25",),  # extend later when more engines land
)
await gateway.register_command_router(command_router)
```

- [ ] **Step 4: Add --interactive flag and ensure one-shot still works**

In `src/xana/cli/xanac.py`, modify `_parse_args` to add the flag:

```python
parser.add_argument("--interactive", "-i", action="store_true",
                    help="enter REPL mode (default when no positional text)")
```

In `_run`, after the existing positional-text branch, leave the one-shot path alone (it already calls `gateway.submit()` which now goes through the command_router automatically).

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_xanac_one_shot_command.py -v && pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/xana/runtime/boot.py src/xana/cli/xanac.py tests/test_xanac_one_shot_command.py
git commit -m "feat(boot,xanac): wire DefaultCommandRouter so one-shot /help works"
```

---

### Task 12: REPL loop with prompt_toolkit

**Files:**
- Modify: `src/xana/cli/xanac.py`
- Test: `tests/test_xanac_repl_loop.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_xanac_repl_loop.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from xana.cli.xanac import _repl_loop


class _FakePrompt:
    """Replaces prompt_toolkit PromptSession in tests — yields scripted lines."""
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def prompt_async(self, *args, **kwargs) -> str:
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


@pytest.mark.asyncio
async def test_repl_loop_dispatches_line_through_gateway(tmp_path: Path) -> None:
    fake_prompt = _FakePrompt(["hello", "/help", ""])
    gateway = AsyncMock()
    gateway.submit.return_value.__aiter__.return_value = []  # zero rendered messages

    async def submit_gen(inbound):
        yield  # type: ignore
    gateway.submit = lambda inbound: _empty_gen()

    submitted: list[str] = []
    class _GW:
        def submit(self, inbound):
            submitted.append(inbound.text)
            async def gen():
                if False:
                    yield None
            return gen()
        async def cancel(self, sid, rid=None): ...

    await _repl_loop(_GW(), prompt_factory=lambda: fake_prompt, session_id="s1")
    assert submitted == ["hello", "/help"]


async def _empty_gen():
    if False:
        yield None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_xanac_repl_loop.py -v`
Expected: fail — `_repl_loop` undefined.

- [ ] **Step 3: Implement REPL loop**

Append to `src/xana/cli/xanac.py`:

```python
from typing import Awaitable, Callable, Protocol


class _PromptLike(Protocol):
    async def prompt_async(self, *args, **kwargs) -> str: ...


async def _repl_loop(
    gateway,
    *,
    prompt_factory: Callable[[], _PromptLike] | None = None,
    session_id: str = "cli-session",
    adapter_id: str = "cli",
) -> int:
    """Interactive REPL: read a line, submit, stream events, loop.

    Ctrl-C on an in-flight turn cancels via gateway.cancel(); Ctrl-C at the
    prompt or Ctrl-D exits cleanly. EOFError from prompt_factory also exits.
    """
    if prompt_factory is None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        prompt = PromptSession("> ")
        prompt_ctx = patch_stdout()
    else:
        prompt = prompt_factory()
        prompt_ctx = _NullCtx()

    with prompt_ctx:
        while True:
            try:
                line = (await prompt.prompt_async()).strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not line:
                continue
            inbound = InboundMessage(adapter_id=adapter_id, session_id=session_id, text=line)
            stream = gateway.submit(inbound)
            inflight = asyncio.create_task(_drain(stream))
            try:
                await inflight
            except KeyboardInterrupt:
                inflight.cancel()
                await gateway.cancel(session_id, None)
                print("\n[xanac] ⏹ 已取消当前 turn。")


async def _drain(stream) -> None:
    async for _ in stream:
        pass


class _NullCtx:
    def __enter__(self) -> None: ...
    def __exit__(self, *a) -> None: ...
```

Also extend `_run` in `xanac.py`:

```python
async def _run(args: argparse.Namespace) -> int:
    project_root = (args.project or Path.cwd()).resolve()
    boot_result = await boot(project_root=project_root)
    adapter = CliAdapter(adapter_id="cli", session_id=args.session)
    await adapter.start()
    await boot_result.gateway.register_adapter("cli", adapter)

    text = " ".join(args.text).strip()
    if not text and not args.interactive:
        # Default to interactive when no text supplied (cc-style).
        args.interactive = True

    if args.interactive:
        return await _repl_loop(boot_result.gateway, session_id=args.session)

    inbound = InboundMessage(adapter_id="cli", session_id=args.session, text=text)
    exit_code = 0
    async for rendered in boot_result.gateway.submit(inbound):
        if rendered.metadata.get("event_kind") == "error":
            exit_code = 1
    return exit_code
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_xanac_repl_loop.py -v && pytest -q`
Expected: all pass

- [ ] **Step 5: Smoke test the REPL manually**

Run: `xanac --session test-repl` then type `/help`, then a line of free text, then Ctrl-C.
Expected: help menu prints, free text runs through chat agent, Ctrl-C exits.

- [ ] **Step 6: Commit**

```bash
git add src/xana/cli/xanac.py tests/test_xanac_repl_loop.py
git commit -m "feat(xanac): interactive REPL mode with /help routing and Ctrl-C exit"
```

---

### Task 13: Ctrl-C cancels in-flight turn (not the REPL)

**Files:**
- Modify: `src/xana/cli/xanac.py` (already updated in task 12, but verify the cancel branch)
- Modify: `src/xana/harness_sdk/default.py` — honor AbortSignal mid-loop
- Test: `tests/test_xanac_cancel_in_flight.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_xanac_cancel_in_flight.py`:

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xana.runtime.boot import boot
from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.cli import CliAdapter


@pytest.mark.asyncio
async def test_gateway_cancel_aborts_chat_agent_send(tmp_path: Path) -> None:
    result = await boot(project_root=tmp_path)
    adapter = CliAdapter(adapter_id="cli", session_id="s1")
    await adapter.start()
    await result.gateway.register_adapter("cli", adapter)

    inbound = InboundMessage(adapter_id="cli", session_id="s1", text="please loop forever")
    async def drain():
        async for _ in result.gateway.submit(inbound):
            pass
    task = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    await result.gateway.cancel("s1", None)
    # If cancel works, the drain task finishes promptly.
    await asyncio.wait_for(task, timeout=1.0)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_xanac_cancel_in_flight.py -v`
Expected: hangs / times out — there is no abort path between turns.

- [ ] **Step 3: Add session-scoped abort tracking to DefaultAgentGateway**

In `src/xana/agent_adapter/gateway.py`, add to `__init__`:

```python
self._cancel_flags: dict[str, bool] = {}
```

Replace `cancel`:

```python
async def cancel(self, session_id: str, run_id: str | None = None) -> None:
    self._cancel_flags[session_id] = True
    for agent in self.agents.values():
        await agent.cancel(session_id, run_id)
```

Add a method:

```python
def is_cancel_requested(self, session_id: str) -> bool:
    return self._cancel_flags.get(session_id, False)

def clear_cancel(self, session_id: str) -> None:
    self._cancel_flags.pop(session_id, None)
```

In `_submit`, before the `async for event in agent.send(request):` loop, call `self.clear_cancel(inbound.session_id)`. After the loop, break early on cancellation if the flag flips:

```python
async for event in agent.send(request):
    rendered = await adapter.render(event)
    yield self._attach_event_kind(rendered, event)
    if self._cancel_flags.get(inbound.session_id):
        break
self.clear_cancel(inbound.session_id)
```

- [ ] **Step 4: Honor AbortSignal in DefaultHarness loop**

In `src/xana/harness_sdk/default.py`, inside the `for turn_index in range(_MAX_TURNS):` loop, at the very top:

```python
if context.abort.aborted:
    yield HarnessEvent(kind="cancelled", session_id=session_id, run_id=run_id,
                       payload={"reason": context.abort.reason or "cancelled"})
    return
```

In ChatAgent's `cancel()` method (locate in `src/xana/agents/chat_agent.py`), set a flag on the agent that on next `send()` call flips `HarnessContext.abort = AbortSignal(aborted=True, reason="user_cancel")` when building the context. Add the field to ChatAgent and have `_build_context` consult it.

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_xanac_cancel_in_flight.py -v && pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/xana/agent_adapter/gateway.py src/xana/harness_sdk/default.py src/xana/agents/chat_agent.py tests/test_xanac_cancel_in_flight.py
git commit -m "feat(gateway,harness): /cancel and Ctrl-C abort in-flight tool loop via AbortSignal"
```

---

### Task 14: ChatAgent forwards SessionConfig to HarnessContext

**Files:**
- Modify: `src/xana/agents/chat_agent.py`
- Modify: `src/xana/agent_adapter/gateway.py` — inject session_config_store
- Test: `tests/test_chat_agent_session_config.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_chat_agent_session_config.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from xana.runtime.boot import boot
from xana.session_config.contracts import SessionConfig
from xana.agent_adapter.contracts import InboundMessage


@pytest.mark.asyncio
async def test_session_config_model_override_is_threaded_into_harness(tmp_path: Path) -> None:
    """After /model deepseek-foo, the next turn's HarnessContext.model == 'deepseek-foo'."""
    result = await boot(project_root=tmp_path)
    await result.session_config_store.save(
        SessionConfig(session_id="s1", model="custom-model", effort="high", streaming=False)
    )
    seen: dict = {}
    original_invoke = result.agents["chat"].harness.invoke

    def spy(request, context):
        seen["model"] = context.model
        seen["effort"] = context.effort
        seen["streaming"] = context.config_snapshot.get("streaming")
        return original_invoke(request, context)
    result.agents["chat"].harness.invoke = spy  # type: ignore[assignment]

    await result.gateway.register_adapter("cli", _NullAdapter())
    inbound = InboundMessage(adapter_id="cli", session_id="s1", text="hi")
    async for _ in result.gateway.submit(inbound):
        break
    assert seen.get("model") == "custom-model"
    assert seen.get("effort") == "high"
    assert seen.get("streaming") is False


class _NullAdapter:
    adapter_id = "cli"
    async def start(self): ...
    async def stop(self): ...
    def receive(self): raise NotImplementedError
    async def render(self, event): 
        from xana.agent_adapter.contracts import RenderedMessage
        return RenderedMessage(adapter_id="cli", session_id=event.session_id, text="")
    async def bind_session(self, b): ...
    async def health(self):
        from xana.agent_adapter.contracts import AdapterHealth
        return AdapterHealth(adapter_id="cli", status="healthy")
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_chat_agent_session_config.py -v`
Expected: fail — model is still the boot default, not "custom-model".

- [ ] **Step 3: Plumb session_config_store into ChatAgent**

In `src/xana/agents/chat_agent.py`, add `session_config_store` parameter to `ChatAgent.__init__` (default `None`). In `_build_context` (or whichever method builds HarnessContext), if `self.session_config_store` is not None, do:

```python
cfg = await self.session_config_store.get(request.session_id)
model = cfg.model or self.model
effort = cfg.effort if cfg.effort is not None else None
config_snapshot = {
    **self._base_config_snapshot,
    "streaming": cfg.streaming,
    "thinking": cfg.thinking,
    "search_provider": cfg.search_provider,
}
```

Use those values in the returned HarnessContext.

In `src/xana/runtime/boot.py`, pass `session_config_store=session_config_store` to `ChatAgent(...)`.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_chat_agent_session_config.py -v && pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/xana/agents/chat_agent.py src/xana/runtime/boot.py tests/test_chat_agent_session_config.py
git commit -m "feat(chat_agent): forward SessionConfig fields into HarnessContext"
```

---

### Task 15: REPL banner + status line on enter

**Files:**
- Modify: `src/xana/cli/xanac.py`

- [ ] **Step 1: Add banner print before REPL loop**

In `src/xana/cli/xanac.py`, before `await _repl_loop(...)` in `_run`, add:

```python
print(f"xanac · interactive · session={args.session}")
print(f"model={boot_result.model}  provider={boot_result.provider}  cwd={project_root}")
print("输入 /help 查看命令；Ctrl-C 取消当前 turn；Ctrl-D 退出。\n")
```

- [ ] **Step 2: Smoke test**

Run: `xanac --session test-repl`
Expected: banner, then `> ` prompt.

- [ ] **Step 3: Commit**

```bash
git add src/xana/cli/xanac.py
git commit -m "feat(xanac): REPL banner shows model/provider/cwd on entry"
```

---

### Task 16: Final regression sweep

- [ ] **Step 1: Run full test suite**

Run: `pytest -q`
Expected: 0 failures.

- [ ] **Step 2: Smoke test all 11 commands**

In a fresh REPL (`xanac --session smoke`):
1. `/help` → menu
2. `/help /model` → single-line model help
3. `/model` → list models
4. `/model deepseek-v4-flash` → "模型切换为 deepseek-v4-flash"
5. `/effort medium` → "effort 切换为 medium"
6. `/streaming off` → "streaming=False"
7. `/thinking on` → "thinking=True"
8. `/search_provider bm25` → "搜索引擎切换为 bm25"
9. `/project /tmp` → "project_root 切换为 /tmp"
10. `/attach` → "attached=True"
11. `hello` (free text) → chat agent reply
12. Ctrl-C during reply → "已取消当前 turn"
13. `/clear` → "上下文已清空"
14. `/compact` → "历史已压缩"
15. `/cancel` → "已请求取消当前 turn"
16. Ctrl-D → exit

- [ ] **Step 3: Inspect persisted config**

Run: `cat ~/.xana/sessions/smoke.json`
Expected: JSON shows model, effort, streaming, thinking, search_provider, project_root, metadata.attached.

- [ ] **Step 4: Tag if everything works**

```bash
git log --oneline -20
```
Confirm the 16 tasks landed as discrete commits.

---

**End of plan.** Total expected commits: ~15.
