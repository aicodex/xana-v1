# xana Extension API (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a declarative Extension API (`src/xana/extension_api/`) — 8 Contributor ABCs + ExtensionData + immutable ExtensionRegistry + PromptAssembler — wired into DefaultHarness behind an optional constructor parameter, with full backward compatibility when no registry is supplied.

**Architecture:** New `extension_api/` package follows xana's contracts/interfaces/implementation discipline: `contracts.py` (frozen dataclasses + ExtensionData), `interfaces.py` (8 Contributor ABCs with no-op default methods), `registry.py` (builder → frozen registry), `assembler.py` (collect → sort → slot → budget), `capabilities.py` (3 runtime-service ABCs). DefaultHarness gains `__init__(extension_registry=None)` — constructor injection, because `Harness.invoke()` is an interface-locked ABC signature and `HarnessContext` is a frozen contract; neither may be edited. `boot()` passes the registry through. Spec: `todo_ai/extension_api.md`; reference design: `/home/xzp/project/best_agent/ds_review/todo_extension_api.md` and `/home/xzp/project/yuxu_backup`.

**Tech Stack:** Python 3.11+, dataclasses, abc, pytest + pytest-asyncio (existing conventions in `tests/test_default_harness.py`).

---

## Hard Constraints (read before any task)

1. **NEVER edit any existing `interfaces.py` or `contracts.py`** — including `src/xana/interfaces.py` (top-level re-export), `src/xana/harness_sdk/interfaces.py`, `src/xana/harness_sdk/contracts.py`, `src/xana/hooks/*`, `src/xana/tools/*`. This plan only **creates new** `src/xana/extension_api/contracts.py` and `src/xana/extension_api/interfaces.py`, which the spec (`todo_ai/extension_api.md`) explicitly mandates. Do NOT add re-exports to `src/xana/interfaces.py`.
2. **Backward compatibility:** with `extension_registry=None`, every existing test must pass unchanged. Known pre-existing failure to ignore: `test_xanac_empty_input_exits_nonzero`.
3. Commit locally only. **Never push.**
4. Run tests with `python -m pytest` from repo root `/home/xzp/project/xana`.

## Design decisions locked in (do not re-litigate)

- **Constructor injection, not `_invoke()` parameter.** The spec sketch says `_invoke()` accepts the registry, but `Harness.invoke(request, context)` is interface-locked and `HarnessContext` is a frozen contract. `DefaultHarness(extension_registry=...)` achieves the same with zero contract edits.
- **8 Contributors** (spec drops yuxu's TokenUsageContributor/TurnItemContributor): ContextContributor, TurnInputContributor, ToolContributor, ToolLifecycleContributor, TurnLifecycleContributor, ThreadLifecycleContributor, ConfigContributor, ApprovalReviewContributor.
- **Distinct method names** `contribute_prompt` / `contribute_turn_input` (yuxu used `contribute` on both — a single class could then never implement both; we diverge deliberately).
- **Two ExtensionData scopes in Phase 1:** thread store (one per `session_id`, held in a dict on DefaultHarness) and turn store (one per `_invoke` run). No session scope yet (YAGNI).
- **TurnInputContributor fragments** are appended as extra system-tail blocks (after memory tail blocks). xana's history lives in ActiveContextStore; mutating the stored user message would corrupt replay.
- **ConfigContributor call site:** DefaultHarness keeps `{session_id: config_snapshot}`; at run start, if the snapshot changed since last run for that session, fire `on_config_changed(previous, new, thread_store)`. (xana has no in-harness config-change event; this is the deterministic equivalent.)
- **ThreadLifecycleContributor:** `on_thread_start` fires on first run for a session_id; `on_thread_stop` fires from a new public `DefaultHarness.close_thread(session_id)` method (the harness loop has no natural teardown point in Phase 1).
- **ApprovalReviewContributor call site:** in `_dispatch_tool`, when `perm.decision != "allow"`, reviewers get a chance to override; first non-None `PermissionResult` replaces `perm`. No reviewers / all None → exact current behavior (`_denied_result`).
- **Extension tools** are merged ephemeral-per-run (schema appended, lookup dict checked before `tool_registry`); they are NOT registered into the shared ToolRegistry (Rule 4: boot() is the only composition point — and the registry is shared across agents).
- Phase 2 (migrating builtin tools/skills to `ext/`, CodeReviewerAgent demo from spec 验证#3) is **out of scope** — spec marks it 可选.

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/xana/extension_api/__init__.py` | Create | empty (Rule 6: no central registration) |
| `src/xana/extension_api/contracts.py` | Create | PromptSlot, SLOT_ORDER, PromptFragment, ContextFragment, TurnInputContext, ExtensionData, 8 lifecycle Input dataclasses |
| `src/xana/extension_api/interfaces.py` | Create | 8 Contributor ABCs |
| `src/xana/extension_api/capabilities.py` | Create | SchedulerService, SandboxManager, AgentSpawner ABCs |
| `src/xana/extension_api/registry.py` | Create | ExtensionRegistry (frozen) + ExtensionRegistryBuilder |
| `src/xana/extension_api/assembler.py` | Create | PromptAssembler |
| `src/xana/harness_sdk/default.py` | Modify | constructor param, lifecycle/prompt/tool wiring, close_thread |
| `src/xana/runtime/boot.py` | Modify | `extension_registry` param → `DefaultHarness(...)` |
| `CLAUDE.md` | Create | 开发规约 (spec Rule 7) |
| `tests/test_extension_contracts.py`, `tests/test_extension_registry.py`, `tests/test_prompt_assembler.py`, `tests/test_harness_extensions.py`, `tests/test_harness_extension_tools.py`, `tests/test_boot_extension_registry.py`, `tests/test_arch_layers.py` | Create | per-task tests |

---

### Task 1: extension_api contracts

**Files:**
- Create: `src/xana/extension_api/__init__.py` (empty file)
- Create: `src/xana/extension_api/contracts.py`
- Test: `tests/test_extension_contracts.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_extension_contracts.py"""
from xana.extension_api.contracts import (
    SLOT_ORDER,
    ContextFragment,
    ExtensionData,
    PromptFragment,
    TurnInputContext,
)


def test_slot_order():
    assert SLOT_ORDER == ("system_head", "system", "system_tail")


def test_prompt_fragment_defaults():
    f = PromptFragment(slot="system", content="hello")
    assert f.priority == 100
    assert f.source == ""


def test_extension_data_type_keyed():
    class Counter:
        def __init__(self) -> None:
            self.n = 0

    data = ExtensionData("run-1")
    assert data.get(Counter) is None
    c = data.get_or_init(Counter, Counter)
    c.n = 5
    assert data.get_or_init(Counter, Counter).n == 5  # same instance
    assert data.get(Counter) is c
    prev = data.insert(Counter())
    assert prev is c
    assert data.remove(Counter).n == 0
    assert data.remove(Counter) is None


def test_turn_input_context_frozen():
    ctx = TurnInputContext(
        session_id="s", agent_name="a", run_id="r", user_input="hi"
    )
    assert ctx.cwd is None
    import pytest
    with pytest.raises(Exception):
        ctx.user_input = "x"  # type: ignore[misc]


def test_context_fragment():
    assert ContextFragment(content="c").source == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extension_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'xana.extension_api'`

- [ ] **Step 3: Write the implementation**

Create empty `src/xana/extension_api/__init__.py`, then:

```python
"""src/xana/extension_api/contracts.py

Extension API data contracts. Frozen dataclasses + ExtensionData (the only
type here with behavior — a type-keyed mutable store, one per scope).

Scopes in Phase 1: thread (one per session_id) and turn (one per harness run).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeVar

from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolInvocation

T = TypeVar("T")

PromptSlot = Literal["system_head", "system", "system_tail"]
SLOT_ORDER: tuple[PromptSlot, ...] = ("system_head", "system", "system_tail")


@dataclass(frozen=True)
class PromptFragment:
    """A system-prompt fragment contributed by a ContextContributor."""

    slot: PromptSlot
    content: str
    priority: int = 100  # lower sorts first within a slot
    source: str = ""


@dataclass(frozen=True)
class ContextFragment:
    """Per-turn context contributed by a TurnInputContributor."""

    content: str
    source: str = ""


@dataclass(frozen=True)
class TurnInputContext:
    session_id: str
    agent_name: str
    run_id: str
    user_input: str
    cwd: str | None = None


class ExtensionData:
    """Type-keyed mutable store. One instance per scope (thread / turn)."""

    def __init__(self, scope_id: str) -> None:
        self.scope_id = scope_id
        self._entries: dict[type, Any] = {}

    def get(self, typ: type[T]) -> T | None:
        return self._entries.get(typ)

    def get_or_init(self, typ: type[T], init: Callable[[], T]) -> T:
        if typ not in self._entries:
            self._entries[typ] = init()
        return self._entries[typ]

    def insert(self, value: T) -> T | None:
        prev = self._entries.get(type(value))
        self._entries[type(value)] = value
        return prev

    def remove(self, typ: type[T]) -> T | None:
        return self._entries.pop(typ, None)


@dataclass(frozen=True)
class ThreadStartInput:
    session_id: str
    thread_store: ExtensionData


@dataclass(frozen=True)
class ThreadStopInput:
    session_id: str
    thread_store: ExtensionData


@dataclass(frozen=True)
class TurnStartInput:
    session_id: str
    agent_name: str
    run_id: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class TurnStopInput:
    session_id: str
    agent_name: str
    run_id: str
    status: str  # "final" | "error"
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class TurnAbortInput:
    session_id: str
    agent_name: str
    run_id: str
    reason: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ToolStartInput:
    session_id: str
    run_id: str
    tool_name: str
    call_id: str
    arguments: dict[str, Any]
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ToolFinishInput:
    session_id: str
    run_id: str
    tool_name: str
    call_id: str
    ok: bool
    error: str | None
    rendered: str
    thread_store: ExtensionData
    turn_store: ExtensionData


@dataclass(frozen=True)
class ApprovalReviewInput:
    invocation: ToolInvocation
    result: PermissionResult  # the non-allow result a reviewer may override
    thread_store: ExtensionData
    turn_store: ExtensionData
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extension_contracts.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/xana/extension_api/__init__.py src/xana/extension_api/contracts.py tests/test_extension_contracts.py
git commit -m "feat(extension_api): data contracts — fragments, lifecycle inputs, ExtensionData"
```

---

### Task 2: Contributor ABCs + capability ABCs

**Files:**
- Create: `src/xana/extension_api/interfaces.py`
- Create: `src/xana/extension_api/capabilities.py`
- Test: `tests/test_extension_contracts.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_extension_contracts.py`)

```python
import asyncio

from xana.extension_api.interfaces import (
    ApprovalReviewContributor,
    ConfigContributor,
    ContextContributor,
    ThreadLifecycleContributor,
    ToolContributor,
    ToolLifecycleContributor,
    TurnInputContributor,
    TurnLifecycleContributor,
)


def test_lifecycle_contributors_have_noop_defaults():
    """Subclasses override only the hooks they need; defaults do nothing."""

    class MyExt(TurnLifecycleContributor, ThreadLifecycleContributor,
                ToolLifecycleContributor, ConfigContributor):
        pass

    ext = MyExt()  # instantiable: defaults are concrete no-ops
    assert asyncio.run(ext.on_turn_start(None)) is None  # type: ignore[arg-type]
    assert asyncio.run(ext.on_thread_stop(None)) is None  # type: ignore[arg-type]
    assert asyncio.run(ext.on_tool_finish(None)) is None  # type: ignore[arg-type]
    assert asyncio.run(ext.on_config_changed({}, {}, None)) is None  # type: ignore[arg-type]


def test_data_contributors_are_abstract():
    import pytest
    for cls in (ContextContributor, TurnInputContributor,
                ToolContributor, ApprovalReviewContributor):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract]


def test_capability_abcs_are_abstract():
    import pytest
    from xana.extension_api.capabilities import (
        AgentSpawner, SandboxManager, SchedulerService,
    )
    for cls in (SchedulerService, SandboxManager, AgentSpawner):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extension_contracts.py -v`
Expected: new tests FAIL with `ModuleNotFoundError` / `ImportError`

- [ ] **Step 3: Write the implementation**

```python
"""src/xana/extension_api/interfaces.py

The 8 Contributor ABCs. Lifecycle contributors ship concrete no-op defaults
(subclasses override only what they need); data-producing contributors are
abstract (a no-op implementation would be meaningless).

Design note: contribute_prompt / contribute_turn_input have distinct names so
one class can implement both (yuxu's reference design used `contribute` twice).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from xana.extension_api.contracts import (
    ApprovalReviewInput,
    ContextFragment,
    ExtensionData,
    PromptFragment,
    ThreadStartInput,
    ThreadStopInput,
    ToolFinishInput,
    ToolStartInput,
    TurnAbortInput,
    TurnInputContext,
    TurnStartInput,
    TurnStopInput,
)
from xana.policy.contracts import PermissionResult
from xana.tools.interfaces import Tool


class ContextContributor(ABC):
    """Contributes system-prompt fragments, assembled by PromptAssembler."""

    @abstractmethod
    async def contribute_prompt(
        self, thread_store: ExtensionData, turn_store: ExtensionData
    ) -> list[PromptFragment]: ...


class TurnInputContributor(ABC):
    """Contributes per-turn context fragments (injected as system-tail blocks)."""

    @abstractmethod
    async def contribute_turn_input(
        self,
        ctx: TurnInputContext,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
    ) -> list[ContextFragment]: ...


class ToolContributor(ABC):
    """Declares tools this extension owns; merged into the run's tool schema."""

    @abstractmethod
    def tools(self) -> list[Tool]: ...


class ToolLifecycleContributor(ABC):
    async def on_tool_start(self, input: ToolStartInput) -> None:
        return None

    async def on_tool_finish(self, input: ToolFinishInput) -> None:
        return None


class TurnLifecycleContributor(ABC):
    async def on_turn_start(self, input: TurnStartInput) -> None:
        return None

    async def on_turn_stop(self, input: TurnStopInput) -> None:
        return None

    async def on_turn_abort(self, input: TurnAbortInput) -> None:
        return None


class ThreadLifecycleContributor(ABC):
    async def on_thread_start(self, input: ThreadStartInput) -> None:
        return None

    async def on_thread_stop(self, input: ThreadStopInput) -> None:
        return None


class ConfigContributor(ABC):
    async def on_config_changed(
        self, previous: dict, new: dict, thread_store: ExtensionData
    ) -> None:
        return None


class ApprovalReviewContributor(ABC):
    """May override a non-allow PermissionResult. Return None to abstain."""

    @abstractmethod
    async def review(self, input: ApprovalReviewInput) -> PermissionResult | None: ...
```

```python
"""src/xana/extension_api/capabilities.py

Runtime Service capability ABCs. Services are harness/runtime-provided and
constructor-injected into extensions (Rule 5: no global singletons).
Phase 1 defines the interfaces only; implementations come with consumers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SchedulerService(ABC):
    @abstractmethod
    async def add(self, *, cron: str, prompt: str, recurring: bool = False) -> str: ...

    @abstractmethod
    async def remove(self, task_id: str) -> None: ...

    @abstractmethod
    async def list_all(self) -> tuple[dict[str, Any], ...]: ...


class SandboxManager(ABC):
    @abstractmethod
    async def run_subprocess(
        self, command: list[str], *, cwd: str | None = None, timeout_s: float | None = None
    ) -> tuple[int, str, str]:
        """Returns (exit_code, stdout, stderr)."""


class AgentSpawner(ABC):
    @abstractmethod
    async def spawn(self, *, agent_name: str, prompt: str, session_id: str) -> str:
        """Returns the run_id of the spawned agent run."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extension_contracts.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/xana/extension_api/interfaces.py src/xana/extension_api/capabilities.py tests/test_extension_contracts.py
git commit -m "feat(extension_api): 8 Contributor ABCs + runtime-service capability ABCs"
```

---

### Task 3: ExtensionRegistry + Builder

**Files:**
- Create: `src/xana/extension_api/registry.py`
- Test: `tests/test_extension_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_extension_registry.py"""
import dataclasses

import pytest

from xana.extension_api.contracts import ExtensionData, PromptFragment
from xana.extension_api.interfaces import (
    ContextContributor,
    TurnLifecycleContributor,
)
from xana.extension_api.registry import ExtensionRegistry, ExtensionRegistryBuilder


class _Prompt(ContextContributor):
    async def contribute_prompt(self, thread_store, turn_store):
        return [PromptFragment(slot="system", content="x")]


class _Both(ContextContributor, TurnLifecycleContributor):
    async def contribute_prompt(self, thread_store, turn_store):
        return []


def test_builder_add_detects_kinds():
    ext = _Both()
    reg = ExtensionRegistryBuilder().add(ext).build()
    assert reg.context == (ext,)
    assert reg.turn_lifecycle == (ext,)
    assert reg.tools == ()


def test_builder_add_rejects_non_contributor():
    with pytest.raises(TypeError):
        ExtensionRegistryBuilder().add(object())


def test_registry_is_frozen():
    reg = ExtensionRegistryBuilder().add(_Prompt()).build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        reg.context = ()  # type: ignore[misc]


def test_registration_order_preserved():
    a, b = _Prompt(), _Prompt()
    reg = ExtensionRegistryBuilder().add(a).add(b).build()
    assert reg.context == (a, b)


def test_empty_registry():
    reg = ExtensionRegistry()
    assert reg.context == () and reg.approval_review == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extension_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'xana.extension_api.registry'`

- [ ] **Step 3: Write the implementation**

```python
"""src/xana/extension_api/registry.py

ExtensionRegistryBuilder collects extensions; build() seals them into a frozen
ExtensionRegistry that the harness loop reads only. add() fans an object out
into every Contributor kind it implements (one class may implement several).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from xana.extension_api.interfaces import (
    ApprovalReviewContributor,
    ConfigContributor,
    ContextContributor,
    ThreadLifecycleContributor,
    ToolContributor,
    ToolLifecycleContributor,
    TurnInputContributor,
    TurnLifecycleContributor,
)


@dataclass(frozen=True)
class ExtensionRegistry:
    context: tuple[ContextContributor, ...] = ()
    turn_input: tuple[TurnInputContributor, ...] = ()
    tools: tuple[ToolContributor, ...] = ()
    tool_lifecycle: tuple[ToolLifecycleContributor, ...] = ()
    turn_lifecycle: tuple[TurnLifecycleContributor, ...] = ()
    thread_lifecycle: tuple[ThreadLifecycleContributor, ...] = ()
    config: tuple[ConfigContributor, ...] = ()
    approval_review: tuple[ApprovalReviewContributor, ...] = ()


_KIND_FIELDS: tuple[tuple[type, str], ...] = (
    (ContextContributor, "context"),
    (TurnInputContributor, "turn_input"),
    (ToolContributor, "tools"),
    (ToolLifecycleContributor, "tool_lifecycle"),
    (TurnLifecycleContributor, "turn_lifecycle"),
    (ThreadLifecycleContributor, "thread_lifecycle"),
    (ConfigContributor, "config"),
    (ApprovalReviewContributor, "approval_review"),
)


class ExtensionRegistryBuilder:
    def __init__(self) -> None:
        self._buckets: dict[str, list] = {name: [] for _, name in _KIND_FIELDS}

    def add(self, extension: object) -> "ExtensionRegistryBuilder":
        matched = False
        for abc_type, name in _KIND_FIELDS:
            if isinstance(extension, abc_type):
                self._buckets[name].append(extension)
                matched = True
        if not matched:
            raise TypeError(
                f"{type(extension).__name__} implements no Contributor ABC"
            )
        return self

    def build(self) -> ExtensionRegistry:
        return ExtensionRegistry(
            **{name: tuple(items) for name, items in self._buckets.items()}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extension_registry.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/xana/extension_api/registry.py tests/test_extension_registry.py
git commit -m "feat(extension_api): ExtensionRegistry builder with kind fan-out"
```

---

### Task 4: PromptAssembler

**Files:**
- Create: `src/xana/extension_api/assembler.py`
- Test: `tests/test_prompt_assembler.py`

Semantics (collect → sort → slot → budget):
1. Collect PromptFragments from all `registry.context` contributors.
2. Stable-sort by `priority` (lower first).
3. Group by slot; iterate slots in `SLOT_ORDER`; consume a shared char budget — a fragment exceeding the remainder is truncated to fit, then the budget is exhausted.
4. Output order: `system_head` chunks, then `base_parts` (the harness's existing memory+skill blocks), then `system` chunks, then `system_tail` chunks; join non-empty parts with `"\n\n"`. Base parts never count against the budget (they're first-class, not extension content).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_prompt_assembler.py"""
import asyncio

from xana.extension_api.assembler import PromptAssembler
from xana.extension_api.contracts import ExtensionData, PromptFragment
from xana.extension_api.interfaces import ContextContributor
from xana.extension_api.registry import ExtensionRegistryBuilder


class _Static(ContextContributor):
    def __init__(self, fragments):
        self._fragments = fragments

    async def contribute_prompt(self, thread_store, turn_store):
        return list(self._fragments)


def _assemble(fragments, base_parts, budget=8000):
    reg = ExtensionRegistryBuilder().add(_Static(fragments)).build()
    asm = PromptAssembler(reg, budget_chars=budget)
    return asyncio.run(
        asm.assemble(
            thread_store=ExtensionData("t"),
            turn_store=ExtensionData("r"),
            base_parts=base_parts,
        )
    )


def test_slot_ordering_around_base():
    out = _assemble(
        [
            PromptFragment(slot="system_tail", content="TAIL"),
            PromptFragment(slot="system_head", content="HEAD"),
            PromptFragment(slot="system", content="MID"),
        ],
        base_parts=["BASE"],
    )
    assert out == "HEAD\n\nBASE\n\nMID\n\nTAIL"


def test_priority_sort_within_slot():
    out = _assemble(
        [
            PromptFragment(slot="system", content="second", priority=200),
            PromptFragment(slot="system", content="first", priority=1),
        ],
        base_parts=[],
    )
    assert out.index("first") < out.index("second")


def test_budget_truncates():
    out = _assemble(
        [
            PromptFragment(slot="system", content="aaaa", priority=1),
            PromptFragment(slot="system", content="bbbb", priority=2),
        ],
        base_parts=["BASE"],
        budget=6,
    )
    assert "aaaa" in out
    assert "bb" in out and "bbbb" not in out  # truncated to remaining 2 chars


def test_base_parts_exempt_from_budget():
    out = _assemble([], base_parts=["X" * 100], budget=1)
    assert "X" * 100 in out


def test_empty_parts_skipped():
    out = _assemble([PromptFragment(slot="system", content="")], base_parts=["", "A"])
    assert out == "A"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prompt_assembler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
"""src/xana/extension_api/assembler.py

PromptAssembler: collect → sort by priority → slot → shared char budget.
base_parts (harness memory/skill blocks) are first-class and budget-exempt.
"""
from __future__ import annotations

from xana.extension_api.contracts import SLOT_ORDER, ExtensionData, PromptFragment
from xana.extension_api.registry import ExtensionRegistry


class PromptAssembler:
    def __init__(self, registry: ExtensionRegistry, budget_chars: int = 8000) -> None:
        self._registry = registry
        self._budget = budget_chars

    async def assemble(
        self,
        *,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
        base_parts: list[str],
    ) -> str:
        fragments: list[PromptFragment] = []
        for contributor in self._registry.context:
            fragments.extend(
                await contributor.contribute_prompt(thread_store, turn_store)
            )
        fragments.sort(key=lambda f: f.priority)  # stable: ties keep add order

        by_slot: dict[str, list[str]] = {slot: [] for slot in SLOT_ORDER}
        remaining = self._budget
        for slot in SLOT_ORDER:
            for f in (x for x in fragments if x.slot == slot):
                if remaining <= 0:
                    break
                chunk = f.content[:remaining]
                remaining -= len(chunk)
                if chunk:
                    by_slot[slot].append(chunk)

        ordered = (
            by_slot["system_head"]
            + list(base_parts)
            + by_slot["system"]
            + by_slot["system_tail"]
        )
        return "\n\n".join(p for p in ordered if p)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_prompt_assembler.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/xana/extension_api/assembler.py tests/test_prompt_assembler.py
git commit -m "feat(extension_api): PromptAssembler — collect, sort, slot, budget"
```

---

### Task 5: DefaultHarness lifecycle + prompt wiring

**Files:**
- Modify: `src/xana/harness_sdk/default.py` (constructor ~line 44-47; system-prompt assembly ~lines 78-102; terminal paths: cancelled ~125-128, stream error, max-turns ~241-248, final ~250-260)
- Test: `tests/test_harness_extensions.py`

Wire: ThreadLifecycle (start lazily, stop via `close_thread`), TurnLifecycle (start at run begin; abort on cancelled; stop with status `"final"` on the final path and `"error"` on stream-error / max-turns paths), ConfigContributor (snapshot diff per session), TurnInputContributor (fragments → extra system-tail blocks), ContextContributor (via PromptAssembler). **Do not touch `Harness` ABC, `HarnessContext`, or any contracts.**

- [ ] **Step 1: Write the failing tests**

Use the existing `_build_context()` pattern from `tests/test_default_harness.py` (copy the helper; it builds InMemory stores + `FakeModelClient` with `ScriptedTurn`). Core test file:

```python
"""tests/test_harness_extensions.py

A RecorderExtension implements all lifecycle + prompt contributor kinds and
records call order. Mirrors the context-building helper in
tests/test_default_harness.py (FakeModelClient / ScriptedTurn / InMemory stores).
"""
import asyncio

from xana.extension_api.contracts import ContextFragment, PromptFragment
from xana.extension_api.interfaces import (
    ConfigContributor,
    ContextContributor,
    ThreadLifecycleContributor,
    TurnInputContributor,
    TurnLifecycleContributor,
)
from xana.extension_api.registry import ExtensionRegistryBuilder
from xana.harness_sdk.default import DefaultHarness

# Copy _build_context / FakeModelClient / ScriptedTurn imports from
# tests/test_default_harness.py — same fakes, do not re-invent.
from tests.test_default_harness import _build_context  # if importable; else inline copy


class RecorderExtension(
    ContextContributor,
    TurnInputContributor,
    TurnLifecycleContributor,
    ThreadLifecycleContributor,
    ConfigContributor,
):
    def __init__(self):
        self.calls: list[str] = []

    async def contribute_prompt(self, thread_store, turn_store):
        self.calls.append("contribute_prompt")
        return [PromptFragment(slot="system_tail", content="EXT-PROMPT")]

    async def contribute_turn_input(self, ctx, thread_store, turn_store):
        self.calls.append("contribute_turn_input")
        return [ContextFragment(content="EXT-TURN-INPUT")]

    async def on_turn_start(self, input):
        self.calls.append("on_turn_start")

    async def on_turn_stop(self, input):
        self.calls.append(f"on_turn_stop:{input.status}")

    async def on_turn_abort(self, input):
        self.calls.append("on_turn_abort")

    async def on_thread_start(self, input):
        self.calls.append("on_thread_start")

    async def on_thread_stop(self, input):
        self.calls.append("on_thread_stop")

    async def on_config_changed(self, previous, new, thread_store):
        self.calls.append("on_config_changed")


async def _run(harness, request, context):
    return [e async for e in harness.invoke(request, context)]


def test_lifecycle_order_and_prompt_injection():
    ext = RecorderExtension()
    registry = ExtensionRegistryBuilder().add(ext).build()
    harness = DefaultHarness(extension_registry=registry)
    request, context, _ = _build_context(text="hello")  # simple text-only turn
    asyncio.run(_run(harness, request, context))

    assert ext.calls[0] == "on_thread_start"
    assert ext.calls[1] == "on_turn_start"
    assert "contribute_turn_input" in ext.calls
    assert "contribute_prompt" in ext.calls
    assert ext.calls[-1] == "on_turn_stop:final"
    # fragments landed in the assembled system message
    system_msg = harness.last_assembled_messages[0]
    assert "EXT-PROMPT" in system_msg.content
    assert "EXT-TURN-INPUT" in system_msg.content


def test_thread_start_fires_once_then_close_thread():
    ext = RecorderExtension()
    registry = ExtensionRegistryBuilder().add(ext).build()
    harness = DefaultHarness(extension_registry=registry)
    request, context, _ = _build_context(text="hello")
    asyncio.run(_run(harness, request, context))
    request2, context2, _ = _build_context(text="again")  # same session_id
    asyncio.run(_run(harness, request2, context2))
    assert ext.calls.count("on_thread_start") == 1
    asyncio.run(harness.close_thread(context.session_id))
    assert ext.calls[-1] == "on_thread_stop"


def test_config_changed_fires_on_snapshot_diff():
    ext = RecorderExtension()
    registry = ExtensionRegistryBuilder().add(ext).build()
    harness = DefaultHarness(extension_registry=registry)
    request, context, _ = _build_context(text="hello")
    asyncio.run(_run(harness, request, context))
    assert "on_config_changed" not in ext.calls  # first run: nothing to diff
    request2, context2, _ = _build_context(text="again")
    context2 = __import__("dataclasses").replace(
        context2, config_snapshot={"model": "other"}
    )
    asyncio.run(_run(harness, request2, context2))
    assert "on_config_changed" in ext.calls


def test_abort_fires_on_turn_abort():
    from xana.harness_sdk.contracts import AbortSignal
    import dataclasses
    ext = RecorderExtension()
    registry = ExtensionRegistryBuilder().add(ext).build()
    harness = DefaultHarness(extension_registry=registry)
    request, context, _ = _build_context(text="hello")
    context = dataclasses.replace(
        context, abort=AbortSignal(aborted=True, reason="user_cancel")
    )
    asyncio.run(_run(harness, request, context))
    assert "on_turn_abort" in ext.calls
    assert not any(c.startswith("on_turn_stop") for c in ext.calls)


def test_no_registry_is_noop():
    harness = DefaultHarness()
    request, context, _ = _build_context(text="hello")
    events = asyncio.run(_run(harness, request, context))
    assert events[-1].kind == "final"
```

Note for implementer: if `from tests.test_default_harness import _build_context` doesn't work (tests not a package), copy the helper + fakes into this file verbatim, adapting the signature so `_build_context(text=...)` returns `(request, context, stores)` with a single scripted text-only model turn. Match whatever `tests/test_default_harness.py` actually provides — read it first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_harness_extensions.py -v`
Expected: FAIL — `DefaultHarness.__init__() got an unexpected keyword argument 'extension_registry'`

- [ ] **Step 3: Implement in `src/xana/harness_sdk/default.py`**

Constructor:

```python
from xana.extension_api.contracts import (
    ExtensionData,
    ThreadStartInput,
    ThreadStopInput,
    TurnAbortInput,
    TurnInputContext,
    TurnStartInput,
    TurnStopInput,
)
from xana.extension_api.registry import ExtensionRegistry


class DefaultHarness(Harness):
    def __init__(self, extension_registry: "ExtensionRegistry | None" = None) -> None:
        self.last_assembled_messages: tuple[ModelMessage, ...] | None = None
        self.usage_accumulator = DefaultUsageAccumulator()
        self._extensions = extension_registry
        self._thread_stores: dict[str, ExtensionData] = {}
        self._config_snapshots: dict[str, dict] = {}

    async def close_thread(self, session_id: str) -> None:
        """Fire on_thread_stop and drop the thread-scoped extension store."""
        store = self._thread_stores.pop(session_id, None)
        self._config_snapshots.pop(session_id, None)
        if self._extensions is None or store is None:
            return
        for c in self._extensions.thread_lifecycle:
            await c.on_thread_stop(ThreadStopInput(session_id=session_id, thread_store=store))
```

In `_invoke`, right after run_id/session_id/agent_name are established (after the RunRecord block):

```python
ext = self._extensions
thread_store: ExtensionData | None = None
turn_store: ExtensionData | None = None
if ext is not None:
    is_new_thread = session_id not in self._thread_stores
    thread_store = self._thread_stores.setdefault(session_id, ExtensionData(session_id))
    turn_store = ExtensionData(run_id)
    if is_new_thread:
        for c in ext.thread_lifecycle:
            await c.on_thread_start(ThreadStartInput(session_id=session_id, thread_store=thread_store))
    previous_cfg = self._config_snapshots.get(session_id)
    new_cfg = dict(context.config_snapshot)
    if previous_cfg is not None and previous_cfg != new_cfg:
        for c in ext.config:
            await c.on_config_changed(previous_cfg, new_cfg, thread_store)
    self._config_snapshots[session_id] = new_cfg
    for c in ext.turn_lifecycle:
        await c.on_turn_start(TurnStartInput(
            session_id=session_id, agent_name=agent_name, run_id=run_id,
            thread_store=thread_store, turn_store=turn_store,
        ))
```

Replace `system_parts = injected_head + skill_blocks + injected_tail` / `system_content = ...` with:

```python
extension_tail: list[str] = []
if ext is not None and ext.turn_input:
    tic = TurnInputContext(
        session_id=session_id, agent_name=agent_name, run_id=run_id,
        user_input=request.user_input, cwd=context.cwd or None,
    )
    for c in ext.turn_input:
        for frag in await c.contribute_turn_input(tic, thread_store, turn_store):
            extension_tail.append(frag.content)
base_parts = injected_head + skill_blocks + injected_tail + extension_tail
if ext is not None and ext.context:
    from xana.extension_api.assembler import PromptAssembler
    system_content = await PromptAssembler(ext).assemble(
        thread_store=thread_store, turn_store=turn_store, base_parts=base_parts,
    )
else:
    system_content = "\n\n".join(p for p in base_parts if p)
```

Add a private helper and call it at every terminal path:

```python
async def _fire_turn_end(self, ext, *, session_id, agent_name, run_id,
                         thread_store, turn_store, status: str | None,
                         abort_reason: str | None = None) -> None:
    if ext is None:
        return
    if abort_reason is not None:
        for c in ext.turn_lifecycle:
            await c.on_turn_abort(TurnAbortInput(
                session_id=session_id, agent_name=agent_name, run_id=run_id,
                reason=abort_reason, thread_store=thread_store, turn_store=turn_store))
        return
    for c in ext.turn_lifecycle:
        await c.on_turn_stop(TurnStopInput(
            session_id=session_id, agent_name=agent_name, run_id=run_id,
            status=status or "final", thread_store=thread_store, turn_store=turn_store))
```

Terminal-path mapping (find each in `_invoke`):
- cancelled-yield path (abort check) → `abort_reason=context.abort.reason or "aborted"` **before** the cancelled yield-and-return
- stream-error return path → `status="error"`
- max-turns-exceeded path → `status="error"`
- final path → `status="final"` **before** yielding the final event

Keep early-return paths *before* the extension block (missing run prerequisites) untouched.

- [ ] **Step 4: Run tests to verify they pass, plus regression**

Run: `python -m pytest tests/test_harness_extensions.py tests/test_default_harness.py tests/test_default_harness_uses_accumulator.py -v`
Expected: all PASS (no existing harness test changes)

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/test_harness_extensions.py
git commit -m "feat(harness): wire extension lifecycle + prompt contributors into DefaultHarness"
```

---

### Task 6: DefaultHarness tool wiring (ToolContributor / ToolLifecycle / ApprovalReview)

**Files:**
- Modify: `src/xana/harness_sdk/default.py` (tool-schema build ~lines 115-118; `_dispatch_tool` ~lines 280-340)
- Test: `tests/test_harness_extension_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_harness_extension_tools.py

Reuses the _EchoTool / _build_context fakes pattern from
tests/test_default_harness.py; scripts one model turn that calls the
extension-provided tool, then a final text turn.
"""
import asyncio

from xana.extension_api.interfaces import (
    ApprovalReviewContributor,
    ToolContributor,
    ToolLifecycleContributor,
)
from xana.extension_api.registry import ExtensionRegistryBuilder
from xana.harness_sdk.default import DefaultHarness
from xana.policy.contracts import PermissionResult

# Read tests/test_default_harness.py first; reuse its _EchoTool and the
# context builder, scripting: turn 1 = tool_call("ext_echo"), turn 2 = text.


class _EchoToolContributor(ToolContributor):
    def __init__(self, tool):
        self._tool = tool

    def tools(self):
        return [self._tool]


class _Recorder(ToolLifecycleContributor):
    def __init__(self):
        self.calls = []

    async def on_tool_start(self, input):
        self.calls.append(("start", input.tool_name, input.call_id))

    async def on_tool_finish(self, input):
        self.calls.append(("finish", input.tool_name, input.ok))


class _AlwaysApprove(ApprovalReviewContributor):
    async def review(self, input):
        return PermissionResult(decision="allow")


def test_extension_tool_in_schema_and_dispatchable():
    """Extension tool appears in tools_schema and executes without being in
    the shared ToolRegistry. Assert the tool_finished event payload is ok and
    the extension tool actually ran."""
    ...


def test_tool_lifecycle_fires_around_dispatch():
    """_Recorder sees ("start", name, call_id) before ("finish", name, True),
    once per tool call, including for registry (non-extension) tools."""
    ...


def test_approval_review_overrides_deny():
    """A tool whose check_permission returns decision="ask" is normally denied
    (perm.decision != "allow"); with _AlwaysApprove registered, execution
    proceeds and the result is ok."""
    ...


def test_approval_review_abstain_keeps_denial():
    """Reviewer returning None → behavior identical to today (denied result
    with failure_kind="policy_forbidden")."""
    ...
```

(Implementer: flesh `...` bodies using the exact fakes from `tests/test_default_harness.py` — read that file first; the four docstrings are the binding spec. For the approval tests, subclass `_EchoTool` overriding `check_permission` to return `PermissionResult(decision="ask", reason="needs approval")`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_harness_extension_tools.py -v`
Expected: FAIL (extension tool not in schema / lifecycle not called)

- [ ] **Step 3: Implement in `src/xana/harness_sdk/default.py`**

Tool schema build — after the existing `tool_registry.list_specs()` packing:

```python
ext_tools: dict[str, Tool] = {}
if ext is not None:
    for contributor in ext.tools:
        for tool in contributor.tools():
            ext_tools[tool.spec().component.name] = tool
    tools_schema = list(tools_schema) + [
        _to_anthropic_tool_schema(t.spec()) for t in ext_tools.values()
    ]
```

`_dispatch_tool` becomes a method-call site passing extension state. Change its signature to accept `ext_tools: dict[str, Tool]`, `ext: ExtensionRegistry | None`, `thread_store`, `turn_store` (threading through from `_invoke`). Inside:

1. Tool lookup: `tool = ext_tools.get(tool_name) or (await context.tool_registry.get(tool_name) if context.tool_registry else None)`. Keep the existing "registry missing" branch only when `ext_tools` also misses (registry missing + extension hit must still dispatch).
2. Wrap the pipeline:

```python
if ext is not None:
    for c in ext.tool_lifecycle:
        await c.on_tool_start(ToolStartInput(
            session_id=session_id, run_id=run_id, tool_name=tool_name,
            call_id=tc["call_id"], arguments=dict(tc["input"]),
            thread_store=thread_store, turn_store=turn_store,
        ))
# ... existing try/except pipeline ...
if ext is not None:
    for c in ext.tool_lifecycle:
        await c.on_tool_finish(ToolFinishInput(
            session_id=session_id, run_id=run_id, tool_name=tool_name,
            call_id=tc["call_id"], ok=result.ok, error=result.error,
            rendered=rendered, thread_store=thread_store, turn_store=turn_store,
        ))
```

(`on_tool_finish` must fire on the exception path too — place it after the try/except, before return.)

3. Approval override — replace `if perm.decision != "allow": result = _denied_result(perm)` with:

```python
if perm.decision != "allow" and ext is not None and ext.approval_review:
    review_input = ApprovalReviewInput(
        invocation=invocation, result=perm,
        thread_store=thread_store, turn_store=turn_store,
    )
    for reviewer in ext.approval_review:
        override = await reviewer.review(review_input)
        if override is not None:
            perm = override
            break
if perm.decision != "allow":
    result = _denied_result(perm)
else:
    ...  # existing pre/execute/post pipeline unchanged
```

Import `ApprovalReviewInput`, `ToolStartInput`, `ToolFinishInput` from `xana.extension_api.contracts`.

- [ ] **Step 4: Run tests + regression**

Run: `python -m pytest tests/test_harness_extension_tools.py tests/test_harness_extensions.py tests/test_default_harness.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/test_harness_extension_tools.py
git commit -m "feat(harness): extension tools, tool lifecycle hooks, approval review override"
```

---

### Task 7: boot() passthrough + end-to-end TestExtension

**Files:**
- Modify: `src/xana/runtime/boot.py` (signature ~line 96-100; `DefaultHarness()` at ~line 153)
- Test: `tests/test_boot_extension_registry.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_boot_extension_registry.py

End-to-end: boot() with a registry whose TestExtension implements all 8
contributor kinds; submit one message through the gateway (pattern from
tests/test_boot_progress_pipeline.py — read it first); assert the prompt /
lifecycle hooks fired. Tool/approval hooks are covered by Task 6 unit tests;
here assert only what one real boot round-trip can exercise.
"""
import asyncio

import pytest

from xana.extension_api.contracts import ContextFragment, PromptFragment
from xana.extension_api.interfaces import (
    ApprovalReviewContributor, ConfigContributor, ContextContributor,
    ThreadLifecycleContributor, ToolContributor, ToolLifecycleContributor,
    TurnInputContributor, TurnLifecycleContributor,
)
from xana.extension_api.registry import ExtensionRegistryBuilder
from xana.runtime.boot import boot


class TestExtension(  # noqa: N801 — spec-mandated name (验证#1)
    ContextContributor, TurnInputContributor, ToolContributor,
    ToolLifecycleContributor, TurnLifecycleContributor,
    ThreadLifecycleContributor, ConfigContributor, ApprovalReviewContributor,
):
    __test__ = False  # not a pytest class

    def __init__(self):
        self.calls: list[str] = []

    async def contribute_prompt(self, thread_store, turn_store):
        self.calls.append("contribute_prompt")
        return [PromptFragment(slot="system_tail", content="E2E-MARKER")]

    async def contribute_turn_input(self, ctx, thread_store, turn_store):
        self.calls.append("contribute_turn_input")
        return [ContextFragment(content="E2E-TURN")]

    def tools(self):
        self.calls.append("tools")
        return []

    async def on_turn_start(self, input): self.calls.append("on_turn_start")
    async def on_turn_stop(self, input): self.calls.append("on_turn_stop")
    async def on_thread_start(self, input): self.calls.append("on_thread_start")
    async def review(self, input):
        self.calls.append("review")
        return None


def test_boot_wires_extension_registry(tmp_path, monkeypatch):
    # Follow tests/test_boot_progress_pipeline.py for the boot fixture
    # (README_ai.md, fake/env model config) and gateway submission.
    ext = TestExtension()
    registry = ExtensionRegistryBuilder().add(ext).build()

    async def _go():
        result = await boot(project_root=tmp_path, extension_registry=registry)
        # submit one inbound message exactly as test_boot_progress_pipeline does,
        # consume all events
        ...

    asyncio.run(_go())
    for hook in ("on_thread_start", "on_turn_start", "contribute_turn_input",
                 "contribute_prompt", "tools", "on_turn_stop"):
        assert hook in ext.calls, hook
```

(Implementer: copy the boot fixture/submission mechanics from `tests/test_boot_progress_pipeline.py` — including however it fakes the model client; the assertions above are the binding spec. If that test needs env/monkeypatching for the provider, replicate it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_boot_extension_registry.py -v`
Expected: FAIL — `boot() got an unexpected keyword argument 'extension_registry'`

- [ ] **Step 3: Implement in `src/xana/runtime/boot.py`**

```python
from xana.extension_api.registry import ExtensionRegistry

async def boot(
    *,
    project_root: Path,
    config_provider: DefaultConfigProvider | None = None,
    extension_registry: ExtensionRegistry | None = None,
) -> BootResult:
```

and at the agent wiring (~line 153): `harness=DefaultHarness(extension_registry=extension_registry),`

- [ ] **Step 4: Run test + full regression**

Run: `python -m pytest tests/test_boot_extension_registry.py -v && python -m pytest -q`
Expected: new test PASS; full suite passes except known `test_xanac_empty_input_exits_nonzero`

- [ ] **Step 5: Commit**

```bash
git add src/xana/runtime/boot.py tests/test_boot_extension_registry.py
git commit -m "feat(boot): optional extension_registry threaded into DefaultHarness"
```

---

### Task 8: CLAUDE.md 开发规约 + architecture-enforcement tests

**Files:**
- Create: `CLAUDE.md` (repo root — confirmed absent)
- Test: `tests/test_arch_layers.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_arch_layers.py — CI enforcement of CLAUDE.md development rules."""
import importlib
import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "xana"


def test_extension_api_never_imports_runtime():
    """extension_api/ may import contracts/interfaces, never implementations."""
    forbidden = ("harness_sdk.default", "xana.runtime", "harness_sdk import default")
    for path in (SRC / "extension_api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in text, f"{path.name} imports implementation: {pattern}"


def test_builtin_never_defines_interfaces():
    """tools/builtin and skills/builtin implement ABCs, never define them."""
    pattern = re.compile(r"^class\s+\w+\(.*\b(ABC|Protocol)\b", re.MULTILINE)
    for pkg in ("tools/builtin", "skills/builtin"):
        root = SRC / pkg
        if not root.exists():
            continue
        for path in root.glob("*.py"):
            assert not pattern.search(path.read_text(encoding="utf-8")), (
                f"{path} defines an ABC/Protocol — interfaces belong in */interfaces.py"
            )


def test_no_global_singleton_getters():
    """Rule 5: construction injection — no `from xana.runtime import get_*`."""
    pattern = re.compile(r"from\s+xana\.runtime[\w.]*\s+import\s+get_\w+")
    for path in SRC.rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), str(path)


def test_extension_api_files_independently_importable():
    """Rule 6: each extension_api file imports standalone in a fresh process."""
    for path in sorted((SRC / "extension_api").glob("*.py")):
        module = f"xana.extension_api.{path.stem}"
        if path.stem == "__init__":
            module = "xana.extension_api"
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{module}: {proc.stderr}"


def test_claude_md_exists_with_rules():
    claude_md = SRC.parent.parent / "CLAUDE.md"
    assert claude_md.exists()
    text = claude_md.read_text(encoding="utf-8")
    assert "xana Development Rules" in text
    assert "boot()" in text
```

- [ ] **Step 2: Run tests to verify the CLAUDE.md one fails**

Run: `python -m pytest tests/test_arch_layers.py -v`
Expected: `test_claude_md_exists_with_rules` FAILS; layer tests should already PASS (if any layer test fails, that's a real violation introduced in Tasks 1-7 — fix the source, not the test)

- [ ] **Step 3: Create `CLAUDE.md`** (spec rule 7 — verbatim rules block at the top)

```markdown
# xana Development Rules (CI-enforced)

这些规则在 CI 里由 `tests/test_arch_layers.py` 强制执行。违反 = 构建失败。

1. 永远使用现有接口。加功能前检查 `src/xana/interfaces.py` 和对应模块的 `interfaces.py`。
2. 接口不够 → 先问用户，不要自己加 ABC。用户同意后在 `*/interfaces.py` 加，不能在实现文件里定义新接口。
3. contracts/interfaces/implementation 三层。新模块必须先写 contracts.py → interfaces.py → 再实现。
4. `boot()` 是唯一组合点，禁止在其他文件调用 `register()`。
5. 构造注入，禁止 `from xana.runtime import get_xxx` 全局单例。
6. 每个文件独立可 import，不依赖同目录其他文件。

## Extension API（src/xana/extension_api/）

- 8 种 Contributor ABC 在 `extension_api/interfaces.py`；数据契约在 `extension_api/contracts.py`。
- Extension 通过 `ExtensionRegistryBuilder().add(ext).build()` 组装，frozen registry 由 `boot(extension_registry=...)` 注入 `DefaultHarness`。
- `extension_registry=None` 时 harness 行为与未引入 Extension API 前完全一致。
- Runtime Service ABC（SchedulerService / SandboxManager / AgentSpawner）在 `extension_api/capabilities.py`，构造注入给 Extension。
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_arch_layers.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/test_arch_layers.py
git commit -m "feat: CLAUDE.md development rules + tests/test_arch_layers.py enforcement"
```

---

### Task 9: Final regression + spec checkbox

- [ ] **Step 1: Full suite**

Run: `python -m pytest -q`
Expected: all pass except known pre-existing `test_xanac_empty_input_exits_nonzero`

- [ ] **Step 2: Verify no locked file touched**

Run: `git log --name-only --oneline main@{u}..HEAD 2>/dev/null || git log --name-only --oneline -15` and confirm no existing `interfaces.py` / `contracts.py` outside `src/xana/extension_api/` appears. If any does, STOP and escalate.

- [ ] **Step 3: Verify backward-compat import surface**

Run: `python -c "from xana.harness_sdk.default import DefaultHarness; DefaultHarness()"`
Expected: no error (zero-arg construction still works)

- [ ] **Step 4: Commit any stragglers** (only if working tree is dirty)

---

## Spec coverage map (todo_ai/extension_api.md → tasks)

| Spec item | Task |
|---|---|
| `extension_api/contracts.py` (ExtensionData, PromptFragment, ContextFragment, TurnInputContext, PromptSlot, SLOT_ORDER) | 1 |
| `extension_api/interfaces.py` 8 Contributor ABCs | 2 |
| `extension_api/capabilities.py` (SchedulerService, SandboxManager, AgentSpawner) | 2 |
| `extension_api/registry.py` (frozen registry + builder) | 3 |
| `extension_api/assembler.py` (collect→sort→slot→budget) | 4 |
| DefaultHarness 接入 (turn start/stop/abort, prompt, config, thread) | 5 |
| DefaultHarness 接入 (tool start/finish, extension tools, approval) | 6 |
| `None` 时行为不变 / boot() 加 registry 参数 | 5-7, 9 |
| 验证#1 TestExtension 全 8 种 hook | 7 (+5/6 unit-level) |
| 验证#2 现有 e2e 全过 | 5, 6, 9 |
| 验证#3 CodeReviewerAgent | 显式延后 — 属 Phase 2 (spec 标注"可选") |
| 防膨胀规约 Rule 7: CLAUDE.md + test_arch_layers.py | 8 |
