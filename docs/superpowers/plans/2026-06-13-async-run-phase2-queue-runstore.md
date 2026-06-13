# 异步 Run Phase 2：write queue 单写者 + Run/Placeholder 数据模型 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入唯一写 main history 的 consumer 循环（`asyncio.Queue` + 单消费者）；`Placeholder` 数据契约；`FileRunStore` 落盘 + per-run JSONL writer + `run_completed` marker；sync 模式下所有 main 写入透明走队列，**行为不变**，为 Phase 3 async 铺路。

**Architecture:** 在 `memory/` 加 `Placeholder` 契约和 `ActiveContextStore` 三个新方法（`snapshot` / `append_placeholder` / `replace_placeholder`，决策 3A）。新增 `memory/main_writer.py`，内部用 `asyncio.Queue`，consumer 串行化 store + mirror 写（决策 2A：不引入新 ABC，concrete helper）。新增 `state/file_runs.py` 实现 `FileRunStore`（JSON record per run）+ `RunJsonlWriter`（按 run 写 `runs/{run_id}.jsonl`，写完追加 `run_completed` marker 行）。boot 把 `store + mirror` 包装成走 queue 的版本注入；harness 代码零改动。

**Tech Stack:** Python 3 / asyncio / 标准库 json / pytest (`pytest.mark.asyncio`)。

**接口改动（已经用户同意，2026-06-13）：**
- `memory/contracts.py` 新增 `Placeholder` dataclass + `PlaceholderStatus` Literal。
- `memory/interfaces.py` `ActiveContextStore` 新增 `snapshot` / `append_placeholder` / `replace_placeholder` 三个 abstract 方法。
- **不改** `harness_sdk/contracts.py`（HarnessContext 字段不动；MainWriter 通过包装 store/mirror 注入，对 harness 透明）。
- **不改** `state/contracts.py` / `state/interfaces.py`（`FileRunStore` 是已有 `RunStore` ABC 的实现；`RunJsonlWriter` 是 concrete helper 不抽 ABC）。
- **不新增** queue / consumer ABC（决策 2A：concrete helper 在 `memory/main_writer.py`）。

**前置阅读：** `docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`（Phase 2 段）、设计源 `/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`（「write_queue 单写者」「Placeholder」「per-run JSONL」节）、已 commit 的 `src/xana/memory/file_history.py`（Phase 1 产物）。

---

## 关键既有事实（实现前必读）

- `MemoryMessage` / `ActiveContext`（`src/xana/memory/contracts.py:35-50`）：现 `ActiveContext.messages: tuple[MemoryMessage, ...]`。Phase 2 引入 Placeholder 后，**`messages` 字段语义不变**——仍只装 `MemoryMessage`。Placeholder 通过新方法 `snapshot()` 返回的混合序列体现，**不**塞进 `messages`。理由：保持 `messages` 类型纯净，避免 Phase 1 测试和现有读路径改动。
- `ActiveContextStore`（`src/xana/memory/interfaces.py:28-42`）现 5 方法：`get` / `set` / `append_message` / `compact` / `clear`。Phase 2 新增 3 方法（abstract）；现存唯一实现 `InMemoryActiveContextStore`（`active_context.py:14-43`）补实现。**没有其它实现，新增 abstract 方法不破坏第三方。**
- `RunStore` ABC（`src/xana/state/interfaces.py:26-46`）现 7 方法。`FileRunStore` 全部实现（含 `list_unfinished` / `list_by_session`）。**不改 ABC。**
- `InMemoryRunStore` 仍是 boot 默认（测试用）；`FileRunStore` 由 boot 在传入 `history_root` 时启用。
- `boot()`（`src/xana/runtime/boot.py:108-208`）：Phase 1 已加 `history_root` 参数，构造 `FileHistoryMirror` + replay。Phase 2 在同段补：
  - 构造 `FileRunStore` 并塞进 `StateRepository`；
  - 构造 `MainWriter`，包装 `active_ctx` + `mirror`，把包装结果传给 `DefaultHarness` / `ChatAgent`；
  - 启动 consumer task；返回 `BootResult` 时把 main_writer 也挂上以便 shutdown。
- harness 同时写两处（`src/xana/harness_sdk/default.py:314-324, 578-588, 617-627`）：`active_context_store.append_message(...)` + `history_mirror.append(...)`。**Phase 2 不改这三处**——它们调用的是包装后的 store/mirror，包装内部 enqueue，consumer 拉队列再调原 store + 原 mirror。harness 完全感知不到。
- 目录布局：
  - main history JSONL：`~/.xana/sessions/{session_id}.jsonl`（Phase 1 已落）。
  - run record JSON：`~/.xana/runs/{run_id}.json`（Phase 2 新，FileRunStore）。
  - per-run JSONL：`~/.xana/sessions/runs/{run_id}.jsonl`（Phase 2 新，run 输出落盘 + 末尾 `run_completed` marker）。
- run_id 生成：参考 scheduler 的 `uuid4().hex[:8]`（短但够区分；FileRunStore 在冲突时抛错，不重试）。
- ROADMAP 跨阶段不变量：main.jsonl 唯一真相源；run JSONL 完成后**Phase 5** 才合入 main 并删除——**Phase 2 只写不消费**（per-run JSONL 落盘，不影响 main）。
- Phase 2 **不实现** async 后台 run；harness 仍是单 task 同步阻塞，consumer 实际上没并发，但代码路径保留以便 Phase 3 直接打开 async。

---

## File Structure

- **Modify** `src/xana/memory/contracts.py` — 新增 `Placeholder` dataclass + `PlaceholderStatus` Literal。
- **Modify** `src/xana/memory/interfaces.py` — `ActiveContextStore` 加 3 abstract 方法。
- **Modify** `src/xana/memory/active_context.py` — `InMemoryActiveContextStore` 补 3 方法实现 + 维护 `_placeholders` 字典。
- **Create** `src/xana/memory/main_writer.py` — `MainWriter`（queue + consumer + 启停）+ `_QueuedActiveContextStore` / `_QueuedHistoryMirror`（包装器）。单文件聚合「main 写入串行化」一组职责。
- **Create** `src/xana/state/file_runs.py` — `FileRunStore`（JSON record per run）+ `RunJsonlWriter`（per-run JSONL 落盘 + `run_completed` marker）+ `new_run_id()` helper。
- **Modify** `src/xana/runtime/boot.py` — 用 `FileRunStore` 替换 `InMemoryRunStore`（传 `history_root` 时）；构造 `MainWriter` 包装 store + mirror；启停 consumer；`BootResult` 加 `main_writer` 字段。
- **Test** `tests/test_placeholder_contract.py`、`tests/test_active_context_placeholder.py`、`tests/test_main_writer.py`、`tests/test_file_run_store.py`、`tests/test_run_jsonl_writer.py`、`tests/test_boot_phase2_wiring.py`。

> 提醒：`tests/` 是独立 git 仓（主仓 `.gitignore` 忽略它）。**测试文件的 `git add`/`commit` 要在 `tests/` 目录内执行**；源码在主仓提交。每个 Task 的 commit 步骤已分别写明。

---

### Task 1: Placeholder 契约

**Files:**
- Modify: `src/xana/memory/contracts.py`
- Test: `tests/test_placeholder_contract.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_placeholder_contract.py
from __future__ import annotations

from pathlib import Path

from xana.memory.contracts import Placeholder


def test_placeholder_defaults() -> None:
    ph = Placeholder(run_id="r1", label="查询股价")
    assert ph.run_id == "r1"
    assert ph.label == "查询股价"
    assert ph.status == "running"
    assert ph.jsonl_path is None
    assert ph.metadata == {}


def test_placeholder_with_jsonl_and_status() -> None:
    ph = Placeholder(
        run_id="r2", label="后台 web", status="completed",
        jsonl_path=Path("/tmp/runs/r2.jsonl"), metadata={"started_ts": 1.0},
    )
    assert ph.status == "completed"
    assert ph.jsonl_path == Path("/tmp/runs/r2.jsonl")
    assert ph.metadata["started_ts"] == 1.0


def test_placeholder_is_frozen() -> None:
    ph = Placeholder(run_id="r3", label="x")
    try:
        ph.run_id = "r4"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Placeholder 应该是 frozen")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_placeholder_contract.py -v`
Expected: FAIL（`ImportError: cannot import name 'Placeholder'`）

- [ ] **Step 3: 加 Placeholder 契约**

在 `src/xana/memory/contracts.py` 顶部 Literal 段（约第 19-32 行附近）追加：

```python
PlaceholderStatus = Literal["running", "completed", "cancelled", "failed"]
```

在 `ReplayResult` 之后（第 68 行末）追加：

```python
@dataclass(frozen=True)
class Placeholder:
    """会话消息序列中的占位符：标记「这里有一个 run，状态见 status」。

    与 MemoryMessage 平级，由 ActiveContextStore.snapshot() 返回为混合序列
    `tuple[MemoryMessage | Placeholder, ...]`。**不**塞进 ActiveContext.messages
    （那里只放 MemoryMessage，保持类型纯净）。
    """

    run_id: str
    label: str
    status: PlaceholderStatus = "running"
    jsonl_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

文件顶部 import 段加：

```python
from pathlib import Path
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_placeholder_contract.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: 提交**

```bash
git add src/xana/memory/contracts.py
git commit -m "feat(memory): Placeholder 契约 + PlaceholderStatus

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_placeholder_contract.py && git commit -m "test: Placeholder 契约默认与 frozen" )
```

---

### Task 2: ActiveContextStore 三个新方法 + InMemory 实现

**Files:**
- Modify: `src/xana/memory/interfaces.py`
- Modify: `src/xana/memory/active_context.py`
- Test: `tests/test_active_context_placeholder.py`

设计语义：
- `snapshot(session_id, agent_name) -> tuple[MemoryMessage | Placeholder, ...]`：返回**消息+占位符**混合序列，按时间顺序。Placeholder 插在 message 之间。**这是 Phase 3 fork 上下文的入口**。
- `append_placeholder(session_id, agent_name, placeholder)`：把 PH 追加到 session 的混合序列末尾。
- `replace_placeholder(run_id, replace_with: tuple[MemoryMessage, ...])`：按 `run_id` 定位 PH，**整体替换**为给定 messages；返回受影响 session 的新 ActiveContext，并把替换 messages 写入 `ActiveContext.messages` 末尾（顺序保留：PH 之前的 messages → 替换 messages → PH 之后的 messages）。**Phase 2 sync 模式下不会被调用**，但行为契约必须明确，便于 Phase 3 直接用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_active_context_placeholder.py
from __future__ import annotations

import pytest

from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.contracts import MemoryMessage, Placeholder


@pytest.mark.asyncio
async def test_snapshot_empty_session() -> None:
    store = InMemoryActiveContextStore()
    snap = await store.snapshot("s1", "chat")
    assert snap == ()


@pytest.mark.asyncio
async def test_snapshot_messages_only() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="a"))
    await store.append_message("s1", "chat", MemoryMessage(role="assistant", content="b"))
    snap = await store.snapshot("s1", "chat")
    assert [m.content for m in snap] == ["a", "b"]


@pytest.mark.asyncio
async def test_append_placeholder_appears_in_snapshot() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="A"))
    await store.append_placeholder("s1", "chat", Placeholder(run_id="r1", label="bg"))
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="B"))
    await store.append_placeholder("s1", "chat", Placeholder(run_id="r2", label="bg2"))

    snap = await store.snapshot("s1", "chat")
    kinds = [type(x).__name__ for x in snap]
    assert kinds == ["MemoryMessage", "Placeholder", "MemoryMessage", "Placeholder"]
    assert snap[1].run_id == "r1"  # type: ignore[union-attr]
    assert snap[3].run_id == "r2"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_replace_placeholder_inserts_messages() -> None:
    store = InMemoryActiveContextStore()
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="A"))
    await store.append_placeholder("s1", "chat", Placeholder(run_id="r1", label="bg"))
    await store.append_message("s1", "chat", MemoryMessage(role="user", content="B"))

    new_ctx = await store.replace_placeholder(
        run_id="r1",
        replace_with=(
            MemoryMessage(role="assistant", content="X"),
            MemoryMessage(role="assistant", content="Y"),
        ),
    )
    assert new_ctx is not None
    snap = await store.snapshot("s1", "chat")
    contents = [m.content for m in snap]
    assert contents == ["A", "X", "Y", "B"]
    # messages 字段（纯 MemoryMessage）也应保持顺序
    assert [m.content for m in new_ctx.messages] == ["A", "X", "Y", "B"]


@pytest.mark.asyncio
async def test_replace_placeholder_unknown_run_id_returns_none() -> None:
    store = InMemoryActiveContextStore()
    result = await store.replace_placeholder(run_id="nope", replace_with=())
    assert result is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_active_context_placeholder.py -v`
Expected: FAIL（`AttributeError: ... has no attribute 'snapshot'`）

- [ ] **Step 3: 给 `ActiveContextStore` ABC 加 3 方法**

在 `src/xana/memory/interfaces.py` `ActiveContextStore` 类末尾（`clear` 之后）追加：

```python
    @abstractmethod
    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple["MemoryMessage | Placeholder", ...]: ...

    @abstractmethod
    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: "Placeholder"
    ) -> None: ...

    @abstractmethod
    async def replace_placeholder(
        self,
        run_id: str,
        replace_with: tuple["MemoryMessage", ...],
    ) -> "ActiveContext | None": ...
```

文件顶部 import 加 `Placeholder`：

```python
from xana.memory.contracts import (
    ActiveContext,
    HistoryRow,
    InjectedBlock,
    MemoryLintResult,
    MemoryMessage,
    MemoryNode,
    Placeholder,  # 新增
    ReplayResult,
    SearchHit,
    SearchQuery,
)
```

并去掉字符串引号（既然已 import）：

```python
    @abstractmethod
    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]: ...

    @abstractmethod
    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None: ...

    @abstractmethod
    async def replace_placeholder(
        self,
        run_id: str,
        replace_with: tuple[MemoryMessage, ...],
    ) -> ActiveContext | None: ...
```

- [ ] **Step 4: 补 `InMemoryActiveContextStore` 实现**

`src/xana/memory/active_context.py` import 段补：

```python
from xana.memory.contracts import ActiveContext, HistoryRow, InjectedBlock, MemoryMessage, Placeholder, ReplayResult
```

`InMemoryActiveContextStore.__init__` 改为：

```python
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ActiveContext] = {}
        # per-(session,agent) 混合序列，按追加顺序记录 MemoryMessage 或 Placeholder。
        # 与 _store[key].messages 是独立视图：前者含 PH，后者只装 MemoryMessage。
        self._snapshots: dict[tuple[str, str], list[MemoryMessage | Placeholder]] = {}
```

修改 `append_message` 末尾，让它同时维护 `_snapshots`：

```python
    async def append_message(self, session_id: str, agent_name: str, message: MemoryMessage) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(
            current,
            messages=current.messages + (message,),
            token_estimate=current.token_estimate + len(message.content),
        )
        self._store[key] = updated
        self._snapshots.setdefault(key, []).append(message)
        return updated
```

`set` 也要重置 snapshot（保持 set 是「全量替换」语义）：

```python
    async def set(self, context: ActiveContext) -> None:
        self._store[(context.session_id, context.agent_name)] = context
        self._snapshots[(context.session_id, context.agent_name)] = list(context.messages)
```

`compact` / `clear` 也要清 snapshot：

```python
    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        key = (session_id, agent_name)
        current = self._store.get(key) or ActiveContext(session_id=session_id, agent_name=agent_name)
        updated = replace(current, summary=summary, messages=())
        self._store[key] = updated
        self._snapshots[key] = []
        return updated

    async def clear(self, session_id: str, agent_name: str) -> None:
        self._store.pop((session_id, agent_name), None)
        self._snapshots.pop((session_id, agent_name), None)
```

类末尾追加 3 个新方法：

```python
    async def snapshot(
        self, session_id: str, agent_name: str
    ) -> tuple[MemoryMessage | Placeholder, ...]:
        return tuple(self._snapshots.get((session_id, agent_name), ()))

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        key = (session_id, agent_name)
        self._snapshots.setdefault(key, []).append(placeholder)
        # 确保 ActiveContext 存在（只是 PH，不动 messages / token_estimate）
        if key not in self._store:
            self._store[key] = ActiveContext(session_id=session_id, agent_name=agent_name)

    async def replace_placeholder(
        self,
        run_id: str,
        replace_with: tuple[MemoryMessage, ...],
    ) -> ActiveContext | None:
        for key, items in self._snapshots.items():
            for idx, item in enumerate(items):
                if isinstance(item, Placeholder) and item.run_id == run_id:
                    items[idx:idx + 1] = list(replace_with)
                    msgs = tuple(x for x in items if isinstance(x, MemoryMessage))
                    current = self._store.get(key) or ActiveContext(
                        session_id=key[0], agent_name=key[1]
                    )
                    updated = replace(
                        current,
                        messages=msgs,
                        token_estimate=sum(len(m.content) for m in msgs),
                    )
                    self._store[key] = updated
                    return updated
        return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_active_context_placeholder.py tests/test_file_memory_replayer.py tests/test_interrupt_detection.py tests/test_boot_persistence.py -v`
Expected: PASS（新 5 个 + Phase 1 不回归）

- [ ] **Step 6: 提交**

```bash
git add src/xana/memory/interfaces.py src/xana/memory/active_context.py
git commit -m "feat(memory): ActiveContextStore 加 snapshot/append_placeholder/replace_placeholder

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_active_context_placeholder.py && git commit -m "test: ActiveContextStore PH 三方法" )
```

---

### Task 3: FileRunStore + new_run_id helper

**Files:**
- Create: `src/xana/state/file_runs.py`
- Test: `tests/test_file_run_store.py`

存储约定：`{root}/{run_id}.json`，每个 run 一个 JSON 文件，整文件 round-trip `RunRecord`（含 `Checkpoint` 和 `UsageSnapshot`）。原子写：先写 `.tmp` 再 `os.replace`（参考 `FileSessionConfigStore`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_file_run_store.py
from __future__ import annotations

import re
from pathlib import Path

import pytest

from xana.state.contracts import Checkpoint, RunRecord, UsageSnapshot
from xana.state.file_runs import FileRunStore, new_run_id


def test_new_run_id_format() -> None:
    rid = new_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", rid)
    assert new_run_id() != rid  # 不同


@pytest.mark.asyncio
async def test_create_and_get(tmp_path: Path) -> None:
    store = FileRunStore(root=tmp_path)
    rec = RunRecord(run_id="abc12345", session_id="s1", agent_name="chat", status="running")
    await store.create(rec)
    got = await store.get("abc12345")
    assert got is not None and got.run_id == "abc12345" and got.status == "running"


@pytest.mark.asyncio
async def test_update_status_and_checkpoint(tmp_path: Path) -> None:
    store = FileRunStore(root=tmp_path)
    await store.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    await store.checkpoint("r1", Checkpoint(name="turn_started", run_id="r1", ts=1.0))
    after = await store.update_status("r1", "completed", {"abort_reason": None})
    assert after.status == "completed"
    assert after.last_checkpoint is not None
    assert after.last_checkpoint.name == "turn_started"


@pytest.mark.asyncio
async def test_update_usage_roundtrip(tmp_path: Path) -> None:
    store = FileRunStore(root=tmp_path)
    await store.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    usage = UsageSnapshot(estimated_input_tokens=10, estimated_output_tokens=20, model="m", provider="p")
    await store.update_usage("r1", usage)
    got = await store.get("r1")
    assert got is not None and got.usage.estimated_input_tokens == 10 and got.usage.model == "m"


@pytest.mark.asyncio
async def test_list_unfinished_and_by_session(tmp_path: Path) -> None:
    store = FileRunStore(root=tmp_path)
    await store.create(RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running"))
    await store.create(RunRecord(run_id="r2", session_id="s1", agent_name="chat", status="completed"))
    await store.create(RunRecord(run_id="r3", session_id="s2", agent_name="chat", status="paused"))

    unfinished = {r.run_id for r in await store.list_unfinished()}
    assert unfinished == {"r1", "r3"}

    s1 = {r.run_id for r in await store.list_by_session("s1")}
    assert s1 == {"r1", "r2"}


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path: Path) -> None:
    await FileRunStore(root=tmp_path).create(
        RunRecord(run_id="r1", session_id="s1", agent_name="chat", status="running")
    )
    got = await FileRunStore(root=tmp_path).get("r1")
    assert got is not None and got.status == "running"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_file_run_store.py -v`
Expected: FAIL（`ModuleNotFoundError: xana.state.file_runs`）

- [ ] **Step 3: 实现 FileRunStore + new_run_id**

创建 `src/xana/state/file_runs.py`：

```python
"""文件落盘的 RunStore：{root}/{run_id}.json + per-run JSONL writer (Task 4)。

new_run_id() 生成 8 字符 hex（与 scheduler 风格一致）；冲突由 FileRunStore.create
抛 FileExistsError 暴露，不内部重试。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from xana.state.contracts import Checkpoint, RunRecord, RunStatus, SessionRecord, UsageSnapshot
from xana.state.interfaces import RunStore


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _record_to_json(rec: RunRecord) -> str:
    d = asdict(rec)
    # asdict 把 Checkpoint / UsageSnapshot 都拆开为 dict；ok 直接 json
    return json.dumps(d, ensure_ascii=False)


def _record_from_json(text: str) -> RunRecord:
    d = json.loads(text)
    cp = d.get("last_checkpoint")
    checkpoint = Checkpoint(**cp) if cp else None
    usage_d = d.get("usage") or {}
    usage = UsageSnapshot(**usage_d)
    return RunRecord(
        run_id=d["run_id"],
        session_id=d["session_id"],
        agent_name=d["agent_name"],
        status=d["status"],
        input_request=d.get("input_request") or {},
        last_checkpoint=checkpoint,
        usage=usage,
        pause_reason=d.get("pause_reason"),
        abort_reason=d.get("abort_reason"),
        error=d.get("error"),
        metadata=d.get("metadata") or {},
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class FileRunStore(RunStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self._root / f"{run_id}.json"

    async def create(self, record: RunRecord) -> None:
        path = self._path(record.run_id)
        if path.exists():
            raise FileExistsError(f"RunRecord 已存在: {record.run_id}")
        _atomic_write(path, _record_to_json(record))

    async def get(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return _record_from_json(path.read_text(encoding="utf-8"))

    async def _read(self, run_id: str) -> RunRecord:
        rec = await self.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        return rec

    async def update_status(
        self, run_id: str, status: RunStatus, fields: dict | None = None
    ) -> RunRecord:
        rec = await self._read(run_id)
        patch: dict = {"status": status}
        if fields:
            patch.update(fields)
        updated = replace(rec, **patch)
        _atomic_write(self._path(run_id), _record_to_json(updated))
        return updated

    async def checkpoint(self, run_id: str, checkpoint: Checkpoint) -> None:
        rec = await self._read(run_id)
        updated = replace(rec, last_checkpoint=checkpoint)
        _atomic_write(self._path(run_id), _record_to_json(updated))

    async def update_usage(self, run_id: str, usage: UsageSnapshot) -> None:
        rec = await self._read(run_id)
        updated = replace(rec, usage=usage)
        _atomic_write(self._path(run_id), _record_to_json(updated))

    async def _iter(self) -> list[RunRecord]:
        out: list[RunRecord] = []
        for p in sorted(self._root.glob("*.json")):
            try:
                out.append(_record_from_json(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    async def list_unfinished(self) -> tuple[RunRecord, ...]:
        return tuple(r for r in await self._iter() if r.status in ("running", "paused"))

    async def list_by_session(self, session_id: str) -> tuple[RunRecord, ...]:
        return tuple(r for r in await self._iter() if r.session_id == session_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_file_run_store.py -v`
Expected: PASS（6 个）

- [ ] **Step 5: 提交**

```bash
git add src/xana/state/file_runs.py
git commit -m "feat(state): FileRunStore + new_run_id

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_file_run_store.py && git commit -m "test: FileRunStore CRUD/list" )
```

---

### Task 4: RunJsonlWriter + run_completed marker

**Files:**
- Modify: `src/xana/state/file_runs.py`（追加 `RunJsonlWriter`）
- Test: `tests/test_run_jsonl_writer.py`

设计语义：per-run JSONL 写入 `{sessions_root}/runs/{run_id}.jsonl`，每行一条 `HistoryRow`（复用 Phase 1 的 `_row_to_json`），末尾追加一行 `{"marker": "run_completed", "ts": ..., "status": "completed|cancelled|failed"}` 作为终止标记。Phase 2 **不消费**，仅写。读路径留给 Phase 5。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_run_jsonl_writer.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.state.file_runs import RunJsonlWriter


def _row(run_id: str, content: str) -> HistoryRow:
    return HistoryRow(
        row_id=f"{run_id}-1",
        session_id="s1",
        agent_name="chat",
        kind="assistant",
        payload=MemoryMessage(role="assistant", content=content),
        ts=0.0,
        metadata={"run_id": run_id},
    )


@pytest.mark.asyncio
async def test_append_creates_per_run_jsonl(tmp_path: Path) -> None:
    writer = RunJsonlWriter(sessions_root=tmp_path)
    await writer.append("r1", _row("r1", "你好"))

    p = tmp_path / "runs" / "r1.jsonl"
    assert p.exists()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["payload"]["content"] == "你好"


@pytest.mark.asyncio
async def test_finalize_writes_run_completed_marker(tmp_path: Path) -> None:
    writer = RunJsonlWriter(sessions_root=tmp_path)
    await writer.append("r1", _row("r1", "a"))
    await writer.finalize("r1", status="completed")

    lines = (tmp_path / "runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    last = json.loads(lines[-1])
    assert last == {"marker": "run_completed", "status": "completed", "ts": last["ts"]}
    assert isinstance(last["ts"], float)


@pytest.mark.asyncio
async def test_finalize_with_status_failed(tmp_path: Path) -> None:
    writer = RunJsonlWriter(sessions_root=tmp_path)
    await writer.finalize("r1", status="failed")
    last = json.loads((tmp_path / "runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["status"] == "failed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_run_jsonl_writer.py -v`
Expected: FAIL（`ImportError: cannot import name 'RunJsonlWriter'`）

- [ ] **Step 3: 在 `file_runs.py` 末尾追加 `RunJsonlWriter`**

文件顶部 import 段补：

```python
import time
from typing import Literal

from xana.memory.contracts import HistoryRow
from xana.memory.file_history import _row_to_json  # 复用 Phase 1 序列化
```

末尾追加：

```python
RunTerminalStatus = Literal["completed", "cancelled", "failed"]


class RunJsonlWriter:
    """每个 run 写一个 {sessions_root}/runs/{run_id}.jsonl；finalize 写终止 marker。"""

    def __init__(self, sessions_root: Path) -> None:
        self._dir = Path(sessions_root) / "runs"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.jsonl"

    async def append(self, run_id: str, row: HistoryRow) -> None:
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(_row_to_json(row) + "\n")

    async def finalize(self, run_id: str, status: RunTerminalStatus) -> None:
        marker = json.dumps(
            {"marker": "run_completed", "status": status, "ts": time.time()},
            ensure_ascii=False,
        )
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(marker + "\n")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_run_jsonl_writer.py tests/test_file_run_store.py -v`
Expected: PASS（3 新 + 6 既有）

- [ ] **Step 5: 提交**

```bash
git add src/xana/state/file_runs.py
git commit -m "feat(state): RunJsonlWriter per-run JSONL + run_completed marker

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_run_jsonl_writer.py && git commit -m "test: RunJsonlWriter append/finalize" )
```

---

### Task 5: MainWriter —— 单写者 queue + consumer + 透明包装

**Files:**
- Create: `src/xana/memory/main_writer.py`
- Test: `tests/test_main_writer.py`

设计语义：
- `MainWriter` 持有一个 `asyncio.Queue[_WriteOp]` 和一个常驻 consumer task。
- 包装器 `_QueuedActiveContextStore` / `_QueuedHistoryMirror` 在调用对应方法时把操作丢进队列，**等 consumer 处理完返回**（用 `asyncio.Future`）。
- consumer 串行 `await` 内部 store/mirror 的真方法 → 设置 future 结果。
- **Phase 2 sync 模式下**只有 harness 一个生产者，consumer 立即处理，行为等价直调。
- Phase 3 多生产者（async 后台 run + 主 run）时，consumer 保证 main 顺序写一致。
- 启停：`MainWriter.start()` 起 consumer task；`MainWriter.aclose()` 发 sentinel + 等 task 退出。
- 暴露 5 个方法（覆盖 Phase 2 用到的全部 main 写路径，其它方法 raise NotImplementedError 暂不代理）：包装 store 的 `append_message` / `append_placeholder` / `replace_placeholder` / `compact` / `clear`；包装 mirror 的 `append`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_main_writer.py
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xana.memory.active_context import InMemoryActiveContextStore, InMemoryHistoryMirror
from xana.memory.contracts import HistoryRow, MemoryMessage, Placeholder
from xana.memory.main_writer import MainWriter


def _row(content: str) -> HistoryRow:
    return HistoryRow(
        row_id="r", session_id="s1", agent_name="chat", kind="user",
        payload=MemoryMessage(role="user", content=content), ts=0.0,
    )


@pytest.mark.asyncio
async def test_writes_pass_through_to_inner() -> None:
    store = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
    writer = MainWriter(inner_store=store, inner_mirror=mirror)
    await writer.start()
    try:
        ctx = await writer.store.append_message("s1", "chat", MemoryMessage(role="user", content="a"))
        await writer.mirror.append(_row("a"))
        assert ctx.messages[0].content == "a"
        assert (await store.get("s1", "chat")).messages[0].content == "a"  # type: ignore[union-attr]
        assert mirror.rows()[0].payload.content == "a"
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_writes_are_serialized() -> None:
    """两个并发生产者，consumer 串行消费 → store 里顺序应等于完成顺序的某个串行序。"""
    store = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
    writer = MainWriter(inner_store=store, inner_mirror=mirror)
    await writer.start()
    try:
        async def write(content: str) -> None:
            await writer.store.append_message("s1", "chat", MemoryMessage(role="user", content=content))

        await asyncio.gather(*(write(str(i)) for i in range(20)))
        ctx = await store.get("s1", "chat")
        assert ctx is not None
        # 20 条都写入；内容集合一致即可（顺序不保证全局，但 store 内部一致）
        assert {m.content for m in ctx.messages} == {str(i) for i in range(20)}
        assert len(ctx.messages) == 20
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_placeholder_methods_routed() -> None:
    store = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
    writer = MainWriter(inner_store=store, inner_mirror=mirror)
    await writer.start()
    try:
        await writer.store.append_message("s1", "chat", MemoryMessage(role="user", content="A"))
        await writer.store.append_placeholder("s1", "chat", Placeholder(run_id="r1", label="bg"))
        new_ctx = await writer.store.replace_placeholder(
            run_id="r1",
            replace_with=(MemoryMessage(role="assistant", content="X"),),
        )
        assert new_ctx is not None
        assert [m.content for m in new_ctx.messages] == ["A", "X"]
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    writer = MainWriter(
        inner_store=InMemoryActiveContextStore(),
        inner_mirror=InMemoryHistoryMirror(),
    )
    await writer.start()
    await writer.aclose()
    await writer.aclose()  # 不抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_main_writer.py -v`
Expected: FAIL（`ModuleNotFoundError: xana.memory.main_writer`）

- [ ] **Step 3: 实现 MainWriter**

创建 `src/xana/memory/main_writer.py`：

```python
"""唯一写 main history 的 consumer 循环 + store/mirror 透明包装。

Phase 2 sync 模式下相当于直调（consumer 立即处理），但代码路径保留，Phase 3
async 后台 run 时多个生产者通过同一个 queue 串行化 main 写入。

不抽 ABC（决策 2A）。MainWriter 是 boot 组装出的 concrete helper，对 harness
透明（harness 拿到的 store/mirror 就是 writer.store / writer.mirror）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from xana.memory.contracts import ActiveContext, HistoryRow, MemoryMessage, Placeholder
from xana.memory.interfaces import ActiveContextStore, HistoryMirror


@dataclass
class _WriteOp:
    coro_factory: Callable[[], Coroutine[Any, Any, Any]]
    fut: asyncio.Future


class _QueuedActiveContextStore(ActiveContextStore):
    def __init__(self, inner: ActiveContextStore, enqueue: Callable[[_WriteOp], Coroutine]) -> None:
        self._inner = inner
        self._enqueue = enqueue

    async def _run(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(_WriteOp(coro_factory=factory, fut=fut))
        return await fut

    # 读路径不走 queue（无写竞争）
    async def get(self, session_id: str, agent_name: str) -> ActiveContext | None:
        return await self._inner.get(session_id, agent_name)

    async def snapshot(self, session_id: str, agent_name: str):
        return await self._inner.snapshot(session_id, agent_name)

    # 写路径走 queue
    async def set(self, context: ActiveContext) -> None:
        await self._run(lambda: self._inner.set(context))

    async def append_message(
        self, session_id: str, agent_name: str, message: MemoryMessage
    ) -> ActiveContext:
        return await self._run(
            lambda: self._inner.append_message(session_id, agent_name, message)
        )

    async def compact(self, session_id: str, agent_name: str, summary: str) -> ActiveContext:
        return await self._run(lambda: self._inner.compact(session_id, agent_name, summary))

    async def clear(self, session_id: str, agent_name: str) -> None:
        await self._run(lambda: self._inner.clear(session_id, agent_name))

    async def append_placeholder(
        self, session_id: str, agent_name: str, placeholder: Placeholder
    ) -> None:
        await self._run(lambda: self._inner.append_placeholder(session_id, agent_name, placeholder))

    async def replace_placeholder(
        self, run_id: str, replace_with: tuple[MemoryMessage, ...]
    ) -> ActiveContext | None:
        return await self._run(lambda: self._inner.replace_placeholder(run_id, replace_with))


class _QueuedHistoryMirror(HistoryMirror):
    def __init__(self, inner: HistoryMirror, enqueue: Callable[[_WriteOp], Coroutine]) -> None:
        self._inner = inner
        self._enqueue = enqueue

    async def append(self, row: HistoryRow) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._enqueue(_WriteOp(coro_factory=lambda: self._inner.append(row), fut=fut))
        await fut

    async def replay(self):
        return await self._inner.replay()

    # 透传 read_rows（File 版才有），保留 Phase 1 replayer 可用
    async def read_rows(self):
        read_rows = getattr(self._inner, "read_rows", None)
        if read_rows is None:
            return ()
        return await read_rows()


class MainWriter:
    """组装 queue + consumer + 包装好的 store/mirror。"""

    _SENTINEL: object = object()

    def __init__(
        self,
        inner_store: ActiveContextStore,
        inner_mirror: HistoryMirror,
    ) -> None:
        self._inner_store = inner_store
        self._inner_mirror = inner_mirror
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self.store: ActiveContextStore = _QueuedActiveContextStore(inner_store, self._enqueue)
        self.mirror: HistoryMirror = _QueuedHistoryMirror(inner_mirror, self._enqueue)

    async def _enqueue(self, op: _WriteOp) -> None:
        await self._queue.put(op)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._consume(), name="xana-main-writer")

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is self._SENTINEL:
                return
            assert isinstance(item, _WriteOp)
            try:
                result = await item.coro_factory()
                if not item.fut.done():
                    item.fut.set_result(result)
            except Exception as exc:
                if not item.fut.done():
                    item.fut.set_exception(exc)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        await self._queue.put(self._SENTINEL)
        await self._task
        self._task = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_main_writer.py -v`
Expected: PASS（4 个）

- [ ] **Step 5: 提交**

```bash
git add src/xana/memory/main_writer.py
git commit -m "feat(memory): MainWriter 单写者 queue + 透明包装 store/mirror

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_main_writer.py && git commit -m "test: MainWriter 写串行化 + PH 路由" )
```

---

### Task 6: boot 组装 —— 启用 FileRunStore + MainWriter（透明接入）

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Test: `tests/test_boot_phase2_wiring.py`

要点：
- 当 `history_root` 非空时：用 `FileRunStore(history_root.parent / "runs")` 替换 `InMemoryRunStore`（与设计目录 `~/.xana/runs/` 对齐）；构造 `MainWriter`、`await start()`；把 `writer.store` / `writer.mirror` 传给后续组件；`BootResult` 加 `main_writer` 字段，调用方负责 `await main_writer.aclose()`。
- `history_root=None` 时：现状不变（无 MainWriter，无 FileRunStore），所有 Phase 1 + 既有测试零回归。
- `RunJsonlWriter` 在 Phase 2 暂不接入 harness 写路径（harness 还没区分 main vs run）；只暴露在 `BootResult.run_jsonl_writer` 字段供 Phase 3 用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_boot_phase2_wiring.py
from __future__ import annotations

from pathlib import Path

import pytest

from xana.memory.main_writer import MainWriter
from xana.runtime.boot import boot
from xana.state.file_runs import FileRunStore, RunJsonlWriter
from xana.state.in_memory import InMemoryRunStore


@pytest.mark.asyncio
async def test_boot_with_history_root_uses_file_run_store(tmp_path: Path) -> None:
    project = tmp_path / "proj"; project.mkdir()
    (project / "README_ai.md").write_text("# m\n", encoding="utf-8")

    result = await boot(project_root=project, history_root=tmp_path / "sessions")
    try:
        assert isinstance(result.state_repository.runs(), FileRunStore)
        assert isinstance(result.main_writer, MainWriter)
        assert isinstance(result.run_jsonl_writer, RunJsonlWriter)
    finally:
        await result.main_writer.aclose()


@pytest.mark.asyncio
async def test_boot_without_history_root_in_memory(tmp_path: Path) -> None:
    project = tmp_path / "proj"; project.mkdir()
    (project / "README_ai.md").write_text("# m\n", encoding="utf-8")

    result = await boot(project_root=project)
    assert isinstance(result.state_repository.runs(), InMemoryRunStore)
    assert result.main_writer is None
    assert result.run_jsonl_writer is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_boot_phase2_wiring.py -v`
Expected: FAIL（`AttributeError: ... 'main_writer'`）

- [ ] **Step 3: 改 boot()**

读 `src/xana/runtime/boot.py`，定位：
- `BootResult` 定义（搜 `class BootResult` 或 `@dataclass` + `BootResult`）。
- Phase 1 那段 `if history_root is not None: mirror = FileHistoryMirror(...)` 分支。
- `InMemoryStateRepository()` / `InMemoryRunStore()` 构造点。

改动 3 处：

(a) `BootResult` 加 2 个 optional 字段（在末尾，保持其它字段位置不动）：

```python
    main_writer: "MainWriter | None" = None
    run_jsonl_writer: "RunJsonlWriter | None" = None
```

import 段加：

```python
from xana.memory.main_writer import MainWriter
from xana.state.file_runs import FileRunStore, RunJsonlWriter
```

(b) Phase 1 那段 `if history_root is not None:` 块改为：

```python
    active_ctx = InMemoryActiveContextStore()
    main_writer: MainWriter | None = None
    run_jsonl_writer: RunJsonlWriter | None = None
    mirror: HistoryMirror
    if history_root is not None:
        history_root = Path(history_root)
        file_mirror = FileHistoryMirror(root=history_root)
        await replay_and_detect_interrupts(file_mirror, active_ctx, FileMemoryReplayer())
        main_writer = MainWriter(inner_store=active_ctx, inner_mirror=file_mirror)
        await main_writer.start()
        active_ctx_public: ActiveContextStore = main_writer.store
        mirror = main_writer.mirror
        run_jsonl_writer = RunJsonlWriter(sessions_root=history_root)
    else:
        mirror = InMemoryHistoryMirror()
        active_ctx_public = active_ctx
```

把后续代码里**所有**传入 `active_ctx` 的地方改成 `active_ctx_public`（搜 `active_ctx` 出现位置；ChatAgent 注入 / HarnessContext 构造 / BootResult 等）。**注意：replay 必须用真 active_ctx（不走 queue），所以 `replay_and_detect_interrupts(... , active_ctx, ...)` 保留传裸版**。

(c) `InMemoryStateRepository()` 构造改为：

```python
    if history_root is not None:
        run_store = FileRunStore(root=history_root.parent / "runs")
        state_repo = _CompositeStateRepository(
            sessions=InMemorySessionStore(),  # Phase 2 暂不落 session 表
            runs=run_store,
        )
    else:
        state_repo = InMemoryStateRepository()
```

读 `state/in_memory.py` 看 `InMemoryStateRepository` 实现，按它的形式在 boot.py 同文件内定义一个本地 `_CompositeStateRepository`（不抽 ABC、不挪到 interfaces）：

```python
from xana.state.interfaces import RunStore, SessionStore, StateRepository
from xana.state.in_memory import InMemorySessionStore

class _CompositeStateRepository(StateRepository):
    def __init__(self, sessions: SessionStore, runs: RunStore) -> None:
        self._sessions = sessions
        self._runs = runs
    def sessions(self) -> SessionStore: return self._sessions
    def runs(self) -> RunStore: return self._runs
```

(d) `BootResult(...)` 实例化补 `main_writer=main_writer, run_jsonl_writer=run_jsonl_writer`。

- [ ] **Step 4: 跑测试确认通过 + Phase 1 不回归**

Run: `PYTHONPATH=src python -m pytest tests/test_boot_phase2_wiring.py tests/test_boot_persistence.py tests/test_serve_history_wiring.py tests/test_file_memory_replayer.py tests/test_interrupt_detection.py -v`
Expected: 全 PASS（新 2 + Phase 1 既有）

- [ ] **Step 5: 提交**

```bash
git add src/xana/runtime/boot.py
git commit -m "feat(boot): 启用 FileRunStore + MainWriter 透明接入（history_root 非空时）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
( cd tests && git add test_boot_phase2_wiring.py && git commit -m "test: boot Phase 2 wiring" )
```

---

### Task 7: serve / xanac shutdown 时关闭 MainWriter

**Files:**
- Modify: `src/xana/cli/serve.py`
- Modify: `src/xana/cli/xanac.py`
- Test: 手动烟测 + 既有 serve/xanac 测试不回归

要点：boot 起了 consumer task；shutdown 不关会泄漏。serve 的 try/finally + xanac 的 `_run` 末尾各加一行 `await result.main_writer.aclose() if result.main_writer else None`。

- [ ] **Step 1: 改 serve.py**

读 `src/xana/cli/serve.py` `_run_inner`，找 boot 调用之后的结构。在 `_run_inner` 最外层 try 的 finally 段（或函数末尾确保关 adapter 处）追加：

```python
        if boot_result.main_writer is not None:
            await boot_result.main_writer.aclose()
```

（具体位置依赖现有清理结构，原则：boot 之后无论怎么退出都要关。）

- [ ] **Step 2: 改 xanac.py**

读 `src/xana/cli/xanac.py` `_run`，在函数末尾 return 前（或 except 路径汇合后的 finally）追加同样一行。

- [ ] **Step 3: 跑既有 serve/xanac 测试 + Phase 1/2 全套**

Run: `PYTHONPATH=src python -m pytest tests/test_serve_history_wiring.py tests/test_boot_persistence.py tests/test_boot_phase2_wiring.py tests/test_main_writer.py -v`
Expected: 全 PASS。

- [ ] **Step 4: 烟测 —— serve 重启后 main 写入仍落盘**

```bash
xana sync                          # 重启 serve
# 在飞书/tg 发一条新消息
ls -la ~/.xana/sessions/*.jsonl    # 看时间戳更新
ls -la ~/.xana/runs/               # 目录已建（Phase 2 还没真写 run record）
```

- [ ] **Step 5: 提交**

```bash
git add src/xana/cli/serve.py src/xana/cli/xanac.py
git commit -m "feat(cli): serve/xanac shutdown 关闭 MainWriter consumer

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- [ ] 全量回归：`PYTHONPATH=src python -m pytest tests/ -q --ignore=tests/test_tool_started_events.py`，绿（除既知 `test_xanac_empty_input_exits_nonzero` 旧失败）。
- [ ] `xana sync`（同步并自动重启 serve）；飞书/tg 烟测对话仍正常 + 重启后 replay 仍恢复（Phase 1 行为不退化）。
- [ ] 主仓 + `tests/` 仓都已提交（`git -C . log --oneline -10` + `git -C tests log --oneline -10`）。
- [ ] 更新 `memory/project_async_run_progress.md`：Phase 2 完成、Phase 3 起需新一轮接口审批（`HarnessContext.async_mode` + `SessionConfig.async_mode`）。
- [ ] 写 Phase 3 plan 时读：本 ROADMAP + ASYNC_RUN_DESIGN.md（Async/双模章节）+**已 commit 的** Phase 2 产物（`main_writer.py` / `file_runs.py` / `boot.py` 当前形态）。

---

## Self-Review（plan 作者已核对）

**1. Spec 覆盖（对照 ROADMAP Phase 2 范围）：**
- write queue + consumer 序列化 main 写入 → Task 5 ✅
- `Placeholder` 契约 → Task 1 ✅
- `ActiveContextStore` 支持 message-or-placeholder → Task 2（snapshot/append/replace）✅
- `FileRunStore` 实现已有 `RunStore` ABC → Task 3 ✅
- per-run JSONL writer + `run_completed` marker → Task 4 ✅
- run_id 生成规范 → Task 3（`new_run_id()`）✅
- sync 模式下所有 main 写入走队列、行为不变 → Task 5（透明包装）+ Task 6（boot 注入）✅

**2. Placeholder 扫描：** 无 TBD / "处理边界情况" 空话；每个改代码步骤都贴了完整代码与精确文件/函数锚点（Task 6 的 boot 改动用"读+定位+替换"的描述形式而非行号，因为 boot.py 在 Phase 1 已改过，行号会漂移；替代措施是给出三个明确的搜索锚点+替换前后对照）。

**3. 类型一致性：** `Placeholder` 字段（`run_id`/`label`/`status`/`jsonl_path`/`metadata`）在 Task 1 定义，Task 2-6 引用一致；`PlaceholderStatus` Literal 值与设计文档一致；`new_run_id()` 返回 8 char hex 在 Task 3 / 6 一致；`MainWriter.store` / `.mirror` 在 Task 5 / 6 / 7 一致。`ActiveContextStore.snapshot` 返回类型在 Task 2 定义 `tuple[MemoryMessage | Placeholder, ...]`，Task 5 包装器透传不改类型。

**4. 范围纪律：** 接口改动仅限用户已同意的 3 项（Placeholder + ActiveContextStore 三方法）；`MainWriter` 是 concrete helper（不抽 ABC，决策 2A）；`HarnessContext` / `SessionConfig` / `HarnessContext.async_mode` / 后台 run / async 都留给 Phase 3。Phase 2 完成后 `_run_inner` 调用栈、harness 写路径代码字面零改动——只是底层 store/mirror 走了 queue。
