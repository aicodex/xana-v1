# 异步 Run Phase 1：JSONL 持久化地基 + replay 恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 xana 的对话历史落盘到 per-session JSONL 文件，并在启动时 replay 重建运行时上下文（含中断检测），为后续异步 Run 阶段提供持久化底座。

**Architecture:** 给已有的 `HistoryMirror` / `MemoryReplayer` ABC 补两个文件实现（`FileHistoryMirror` 写 `{root}/{session_id}.jsonl`，`FileMemoryReplayer` 读回重建 `ActiveContext`）。`boot()` 新增可选 `history_root` 参数：传了就用 File 版 + 启动 replay，不传保持现状 InMemory（所有现存测试不受影响）。serve 传入 `~/.xana/sessions` 启用持久化。**纯 sync，不引入 Placeholder / write queue / async。**

**Tech Stack:** Python 3 / asyncio / 标准库 json / pytest（`pytest.mark.asyncio`）。

**接口改动：无。** 只新增已有 ABC 的实现 + boot 组装。如实现中发现现有 ABC 不够用，停下问用户，不要自行加 ABC（见 CLAUDE.md 规则 2）。

**前置阅读：** `docs/superpowers/plans/2026-06-13-async-run-ROADMAP.md`（Phase 1 段）、设计源 `/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`（「会话恢复」「目录结构」「启动恢复流程」节）。

---

## 关键既有事实（实现前必读，避免猜测）

- `HistoryRow`（`src/xana/memory/contracts.py:53-61`）字段：`row_id: str`、`session_id: str`、`agent_name: str`、`kind: HistoryKind`（"user"|"assistant"|"tool"|"system"|"compact"）、`payload: MemoryMessage`、`ts: float`、`metadata: dict`。
- `MemoryMessage`（同文件 35-39）：`role: str`、`content: str`、`metadata: dict`。thinking 存在 `payload.metadata["reasoning_content"]`，工具信息存在 `payload.metadata`（`tool_use_id`/`tool_name`/`ok`/`revealed_tools`）。**序列化必须忠实 round-trip 这三个字段，不丢 metadata。**
- `HistoryMirror` ABC（`src/xana/memory/interfaces.py:45-50`）：`async append(row)` + `async replay() -> ReplayResult`。
- `MemoryReplayer` ABC（同文件 53-55）：`async rebuild_active_context(mirror, store) -> ReplayResult`。
- `ActiveContextStore` ABC（同文件 28-42）：`get` / `set` / `append_message` / `compact` / `clear`。
- `ReplayResult`（contracts 64-68）：`loaded_sessions: int`、`loaded_rows: int`、`errors: tuple[str,...]=()`。
- `InMemoryHistoryMirror`（`src/xana/memory/active_context.py:46-58`）有一个非接口方法 `rows() -> tuple[HistoryRow,...]`。**我们的 File 版用 `async read_rows()`**（async，因为要读盘）——`FileMemoryReplayer` 通过它取行。
- 现 harness 同时写 `active_context_store.append_message(...)` 和 `history_mirror.append(...)`（`src/xana/harness_sdk/default.py:314-324, 578-588, 617-627`）。Phase 1 不改 harness——它已经在写 mirror，我们只是把 mirror 换成会落盘的实现。
- 现 boot（`src/xana/runtime/boot.py:124-125`）硬编码 `active_ctx = InMemoryActiveContextStore()` / `mirror = InMemoryHistoryMirror()`，并把 `mirror` 传给 ChatAgent（189-205）+ 放进 `BootResult`（223-240）。
- 现 serve（`src/xana/cli/serve.py:189-197`）在 `_run_inner` 里 `boot_result = await boot(project_root=..., config_provider=cp)`；`_xana_home()` 返回 `~/.xana`（可被 `$XANA_HOME` 覆盖）。
- 现 xanac 默认 session_id 为 `"cli-session"`。
- 目录布局（设计指定）：`{root}/{session_id}.jsonl`，`root = ~/.xana/sessions`。`FileSessionConfigStore` 已用同目录写 `{sid}.json`——扩展名不同（`.jsonl` vs `.json`），`read_rows()` 只 glob `*.jsonl`，互不干扰。`runs/` 兄弟目录留给 Phase 2，本阶段不创建（YAGNI）。

---

## File Structure

- **Create** `src/xana/memory/file_history.py` — `FileHistoryMirror`（JSONL 落盘 mirror）、`FileMemoryReplayer`（读回重建 ActiveContext）、`needs_continue()` + `replay_and_detect_interrupts()`（中断检测）。单文件聚合「持久化 + 恢复」一组职责，独立可 import。
- **Modify** `src/xana/runtime/boot.py` — 新增 `history_root: Path | None = None` 参数；传了就 File mirror + 启动恢复。
- **Modify** `src/xana/cli/serve.py` — `boot(...)` 调用加 `history_root=_xana_home() / "sessions"`。
- **Modify** `src/xana/cli/xanac.py` — 交互式会话同样持久化（传 `history_root`）。
- **Test** `tests/test_file_history_mirror.py`、`tests/test_file_memory_replayer.py`、`tests/test_interrupt_detection.py`、`tests/test_boot_persistence.py`、`tests/test_serve_history_wiring.py`。

> 提醒：`tests/` 是独立 git 仓（主仓 `.gitignore` 忽略它）。**测试文件的 `git add`/`commit` 要在 `tests/` 目录内执行**；源码改动在主仓提交。每个 Task 的 commit 步骤已分别写明。

---

### Task 1: FileHistoryMirror —— JSONL 序列化 + 追加 + 读回

**Files:**
- Create: `src/xana/memory/file_history.py`
- Test: `tests/test_file_history_mirror.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_file_history_mirror.py
from __future__ import annotations

from pathlib import Path

import pytest

from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.memory.file_history import FileHistoryMirror


def _row(session_id: str, kind: str, role: str, content: str, **meta) -> HistoryRow:
    return HistoryRow(
        row_id=f"{session_id}:{kind}",
        session_id=session_id,
        agent_name="chat",
        kind=kind,  # type: ignore[arg-type]
        payload=MemoryMessage(role=role, content=content, metadata=dict(meta)),
        ts=0.0,
    )


@pytest.mark.asyncio
async def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "user", "你好"))
    await mirror.append(_row("s1", "assistant", "assistant", "在", reasoning_content="想一下"))

    rows = await mirror.read_rows()
    assert [r.payload.role for r in rows] == ["user", "assistant"]
    assert rows[0].payload.content == "你好"
    assert rows[1].payload.metadata["reasoning_content"] == "想一下"
    assert rows[1].kind == "assistant"


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path: Path) -> None:
    await FileHistoryMirror(root=tmp_path).append(_row("s1", "user", "user", "持久"))
    rows = await FileHistoryMirror(root=tmp_path).read_rows()
    assert len(rows) == 1 and rows[0].payload.content == "持久"


@pytest.mark.asyncio
async def test_corrupt_line_skipped(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "user", "好的"))
    with (tmp_path / "s1.jsonl").open("a", encoding="utf-8") as f:
        f.write("{ 这不是合法 json\n")
    rows = await mirror.read_rows()  # 损坏行被跳过，不抛
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_replay_counts(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "user", "a"))
    await mirror.append(_row("s2", "user", "user", "b"))
    result = await mirror.replay()
    assert result.loaded_sessions == 2
    assert result.loaded_rows == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_file_history_mirror.py -v`
Expected: FAIL（`ModuleNotFoundError: xana.memory.file_history`）

- [ ] **Step 3: 实现 FileHistoryMirror**

```python
# src/xana/memory/file_history.py
"""会话历史的 JSONL 持久化 + 启动恢复。

FileHistoryMirror：每 session 一个 {root}/{session_id}.jsonl，每行一条
序列化的 HistoryRow，append-only，跨进程重启不丢。
FileMemoryReplayer：读回 JSONL，重建 ActiveContext（Task 2）。
needs_continue / replay_and_detect_interrupts：CC 风格中断检测（Task 3）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from xana.memory.contracts import HistoryRow, MemoryMessage, ReplayResult
from xana.memory.interfaces import HistoryMirror

_logger = logging.getLogger(__name__)


def _row_to_json(row: HistoryRow) -> str:
    return json.dumps(
        {
            "row_id": row.row_id,
            "session_id": row.session_id,
            "agent_name": row.agent_name,
            "kind": row.kind,
            "ts": row.ts,
            "payload": {
                "role": row.payload.role,
                "content": row.payload.content,
                "metadata": row.payload.metadata,
            },
            "metadata": row.metadata,
        },
        ensure_ascii=False,
    )


def _row_from_json(line: str) -> HistoryRow:
    d = json.loads(line)
    p = d["payload"]
    return HistoryRow(
        row_id=d["row_id"],
        session_id=d["session_id"],
        agent_name=d["agent_name"],
        kind=d["kind"],
        payload=MemoryMessage(
            role=p["role"], content=p["content"], metadata=p.get("metadata") or {}
        ),
        ts=d.get("ts", 0.0),
        metadata=d.get("metadata") or {},
    )


class FileHistoryMirror(HistoryMirror):
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    async def append(self, row: HistoryRow) -> None:
        line = _row_to_json(row)
        with self._path(row.session_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def read_rows(self) -> tuple[HistoryRow, ...]:
        rows: list[HistoryRow] = []
        for path in sorted(self._root.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(_row_from_json(raw))
                except Exception as exc:  # 损坏行容忍，不阻断恢复
                    _logger.warning(
                        "FileHistoryMirror: 跳过损坏行 %s: %s", path.name, exc
                    )
        return tuple(rows)

    async def replay(self) -> ReplayResult:
        rows = await self.read_rows()
        sessions = {r.session_id for r in rows}
        return ReplayResult(loaded_sessions=len(sessions), loaded_rows=len(rows))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_file_history_mirror.py -v`
Expected: PASS（4 个）

- [ ] **Step 5: 提交**

```bash
# 源码（主仓）
git add src/xana/memory/file_history.py
git commit -m "feat(memory): FileHistoryMirror 落盘会话历史到 per-session JSONL

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
# 测试（独立仓）
( cd tests && git add test_file_history_mirror.py && git commit -m "test: FileHistoryMirror 序列化与读回" )
```

---

### Task 2: FileMemoryReplayer —— 从 JSONL 重建 ActiveContext

**Files:**
- Modify: `src/xana/memory/file_history.py`（追加 `FileMemoryReplayer`）
- Test: `tests/test_file_memory_replayer.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_file_memory_replayer.py
from __future__ import annotations

from pathlib import Path

import pytest

from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.memory.file_history import FileHistoryMirror, FileMemoryReplayer


def _row(session_id: str, role: str, content: str) -> HistoryRow:
    return HistoryRow(
        row_id=f"{session_id}:{role}",
        session_id=session_id,
        agent_name="chat",
        kind=role,  # type: ignore[arg-type]
        payload=MemoryMessage(role=role, content=content),
        ts=0.0,
    )


@pytest.mark.asyncio
async def test_rebuild_groups_and_orders(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "问题"))
    await mirror.append(_row("s1", "assistant", "回答"))

    store = InMemoryActiveContextStore()
    result = await FileMemoryReplayer().rebuild_active_context(mirror, store)

    assert result.loaded_sessions == 1
    assert result.loaded_rows == 2
    ctx = await store.get("s1", "chat")
    assert ctx is not None
    assert [m.content for m in ctx.messages] == ["问题", "回答"]
    assert ctx.token_estimate == len("问题") + len("回答")


@pytest.mark.asyncio
async def test_rebuild_empty_root_is_noop(tmp_path: Path) -> None:
    store = InMemoryActiveContextStore()
    result = await FileMemoryReplayer().rebuild_active_context(
        FileHistoryMirror(root=tmp_path), store
    )
    assert result.loaded_rows == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_file_memory_replayer.py -v`
Expected: FAIL（`ImportError: cannot import name 'FileMemoryReplayer'`）

- [ ] **Step 3: 实现 FileMemoryReplayer**

在 `src/xana/memory/file_history.py` 顶部 import 追加：

```python
from xana.memory.contracts import ActiveContext
from xana.memory.interfaces import ActiveContextStore, MemoryReplayer
```

文件末尾追加：

```python
class FileMemoryReplayer(MemoryReplayer):
    """读 mirror 全部行，按 (session_id, agent_name) 分组重建 ActiveContext。

    依赖 mirror 暴露 async read_rows()（FileHistoryMirror / InMemory 均可加）；
    没有该方法的 mirror 视为无可恢复内容。
    """

    async def rebuild_active_context(
        self, mirror: HistoryMirror, store: ActiveContextStore
    ) -> ReplayResult:
        read_rows = getattr(mirror, "read_rows", None)
        if read_rows is None:
            return ReplayResult(loaded_sessions=0, loaded_rows=0)
        rows = await read_rows()
        grouped: dict[tuple[str, str], list[MemoryMessage]] = {}
        for row in rows:
            grouped.setdefault((row.session_id, row.agent_name), []).append(row.payload)
        for (session_id, agent_name), msgs in grouped.items():
            await store.set(
                ActiveContext(
                    session_id=session_id,
                    agent_name=agent_name,
                    messages=tuple(msgs),
                    token_estimate=sum(len(m.content) for m in msgs),
                )
            )
        return ReplayResult(loaded_sessions=len(grouped), loaded_rows=len(rows))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_file_memory_replayer.py -v`
Expected: PASS（2 个）

- [ ] **Step 5: 提交**

```bash
git add src/xana/memory/file_history.py
git commit -m "feat(memory): FileMemoryReplayer 从 JSONL 重建 ActiveContext

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
( cd tests && git add test_file_memory_replayer.py && git commit -m "test: FileMemoryReplayer 分组重建" )
```

---

### Task 3: 中断检测 —— 末尾是 user/tool 则注入 Continue

**Files:**
- Modify: `src/xana/memory/file_history.py`（追加 `needs_continue` + `replay_and_detect_interrupts` + 常量）
- Test: `tests/test_interrupt_detection.py`

设计依据：`ASYNC_RUN_DESIGN.md` 「中断检测（抄 CC）」——replay 后若最后一条是 user 或 tool_result（说明上次没回复完就中断），注入一条 `Continue from where you left off.` 的 user 消息，让模型续上。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_interrupt_detection.py
from __future__ import annotations

from pathlib import Path

import pytest

from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.contracts import HistoryRow, MemoryMessage
from xana.memory.file_history import (
    CONTINUE_TEXT,
    FileHistoryMirror,
    FileMemoryReplayer,
    needs_continue,
    replay_and_detect_interrupts,
)


def test_needs_continue_rules() -> None:
    assert needs_continue((MemoryMessage(role="user", content="x"),)) is True
    assert needs_continue((MemoryMessage(role="tool", content="x"),)) is True
    assert needs_continue((MemoryMessage(role="assistant", content="x"),)) is False
    assert needs_continue(()) is False


def _row(session_id: str, role: str, content: str) -> HistoryRow:
    return HistoryRow(
        row_id=f"{session_id}:{role}", session_id=session_id, agent_name="chat",
        kind=role, payload=MemoryMessage(role=role, content=content), ts=0.0,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_injects_continue_for_trailing_user(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "没人回我"))
    store = InMemoryActiveContextStore()

    await replay_and_detect_interrupts(mirror, store, FileMemoryReplayer())

    ctx = await store.get("s1", "chat")
    assert ctx is not None
    assert ctx.messages[-1].role == "user"
    assert ctx.messages[-1].content == CONTINUE_TEXT


@pytest.mark.asyncio
async def test_no_inject_when_trailing_assistant(tmp_path: Path) -> None:
    mirror = FileHistoryMirror(root=tmp_path)
    await mirror.append(_row("s1", "user", "在吗"))
    await mirror.append(_row("s1", "assistant", "在"))
    store = InMemoryActiveContextStore()

    await replay_and_detect_interrupts(mirror, store, FileMemoryReplayer())

    ctx = await store.get("s1", "chat")
    assert ctx is not None
    assert [m.content for m in ctx.messages] == ["在吗", "在"]  # 未注入
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_interrupt_detection.py -v`
Expected: FAIL（`ImportError: cannot import name 'CONTINUE_TEXT'`）

- [ ] **Step 3: 实现中断检测**

在 `src/xana/memory/file_history.py` 末尾追加：

```python
CONTINUE_TEXT = "Continue from where you left off."


def needs_continue(messages: tuple[MemoryMessage, ...]) -> bool:
    """最后一条是 user 或 tool（上次回合未收尾）→ 需要注入 Continue。"""
    if not messages:
        return False
    return messages[-1].role in ("user", "tool")


async def replay_and_detect_interrupts(
    mirror: HistoryMirror,
    store: ActiveContextStore,
    replayer: MemoryReplayer,
) -> ReplayResult:
    """启动恢复：replay 重建 ActiveContext，再对每个被中断的 session 注入 Continue。

    用 read_rows() 拿到 (session_id, agent_name) 全集（replayer 只回计数），
    逐个检查重建后的尾部，需要时 append 一条 Continue user 消息。
    """
    result = await replayer.rebuild_active_context(mirror, store)
    read_rows = getattr(mirror, "read_rows", None)
    if read_rows is None:
        return result
    keys = {(r.session_id, r.agent_name) for r in await read_rows()}
    for session_id, agent_name in keys:
        ctx = await store.get(session_id, agent_name)
        if ctx is not None and needs_continue(ctx.messages):
            await store.append_message(
                session_id, agent_name,
                MemoryMessage(role="user", content=CONTINUE_TEXT),
            )
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_interrupt_detection.py -v`
Expected: PASS（3 个）

- [ ] **Step 5: 提交**

```bash
git add src/xana/memory/file_history.py
git commit -m "feat(memory): 启动恢复的中断检测，末尾 user/tool 注入 Continue

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
( cd tests && git add test_interrupt_detection.py && git commit -m "test: 中断检测注入 Continue" )
```

---

### Task 4: boot() 接入 history_root（可选持久化）

**Files:**
- Modify: `src/xana/runtime/boot.py`
- Test: `tests/test_boot_persistence.py`

要点：新增 `history_root: Path | None = None` 参数。`None` → 现状 `InMemoryHistoryMirror`（所有现存测试不变）。给路径 → `FileHistoryMirror(root)` + 启动 `replay_and_detect_interrupts(...)` 把磁盘历史灌进 `active_ctx`。`BootResult` 结构不变（`history_mirror` 字段照填）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_boot_persistence.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xana.runtime.boot import boot


@pytest.mark.asyncio
async def test_boot_with_history_root_replays(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "README_ai.md").write_text("# mem\n", encoding="utf-8")

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # 预置一条历史：上次用户问了但没回复
    (sessions / "cli-session.jsonl").write_text(
        json.dumps({
            "row_id": "cli-session-1:user", "session_id": "cli-session",
            "agent_name": "chat", "kind": "user", "ts": 0.0,
            "payload": {"role": "user", "content": "上次的问题", "metadata": {}},
            "metadata": {},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = await boot(project_root=project, history_root=sessions)

    ctx = await result.active_context_store.get("cli-session", "chat")
    assert ctx is not None
    assert ctx.messages[0].content == "上次的问题"
    # 末尾是 user → 注入 Continue
    assert ctx.messages[-1].content == "Continue from where you left off."


@pytest.mark.asyncio
async def test_boot_without_history_root_is_in_memory(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "README_ai.md").write_text("# mem\n", encoding="utf-8")

    result = await boot(project_root=project)  # 不传 history_root
    # 行为不变：无预置历史，空上下文
    assert await result.active_context_store.get("cli-session", "chat") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_boot_persistence.py -v`
Expected: FAIL（`TypeError: boot() got an unexpected keyword argument 'history_root'`）

- [ ] **Step 3: 改 boot()**

`src/xana/runtime/boot.py` import 段（约 36-47 行附近，与现有 memory import 同组）追加：

```python
from xana.memory.file_history import FileHistoryMirror, FileMemoryReplayer, replay_and_detect_interrupts
```

改 `boot` 签名（现 108-113 行）：

```python
async def boot(
    *,
    project_root: Path,
    config_provider: DefaultConfigProvider | None = None,
    extension_registry: ExtensionRegistry | None = None,
    history_root: Path | None = None,
) -> BootResult:
```

把现有的 `active_ctx` / `mirror` 构造（现 124-125 行）：

```python
    active_ctx = InMemoryActiveContextStore()
    mirror = InMemoryHistoryMirror()
```

替换为：

```python
    active_ctx = InMemoryActiveContextStore()
    mirror: HistoryMirror
    if history_root is not None:
        mirror = FileHistoryMirror(root=Path(history_root))
        await replay_and_detect_interrupts(mirror, active_ctx, FileMemoryReplayer())
    else:
        mirror = InMemoryHistoryMirror()
```

（`HistoryMirror` 已在现 import 中，见 boot.py:41-47。）

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_boot_persistence.py tests/test_boot_session_config.py -v`
Expected: PASS（新 2 个 + 既有 boot 测试不回归）

- [ ] **Step 5: 提交**

```bash
git add src/xana/runtime/boot.py
git commit -m "feat(boot): 可选 history_root，启用 FileHistoryMirror + 启动 replay 恢复

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
( cd tests && git add test_boot_persistence.py && git commit -m "test: boot history_root 持久化与恢复" )
```

---

### Task 5: serve 接入持久化

**Files:**
- Modify: `src/xana/cli/serve.py`
- Test: `tests/test_serve_history_wiring.py`

要点：`_run_inner`（serve.py:189-197）里的 `boot(...)` 调用补 `history_root=_xana_home() / "sessions"`，让常驻 bot 的会话落盘 + 重启恢复。

- [ ] **Step 1: 写失败测试**（用 monkeypatch 截获 boot 入参，避免真起网络）

```python
# tests/test_serve_history_wiring.py
from __future__ import annotations

from pathlib import Path

import pytest

import xana.cli.serve as serve


@pytest.mark.asyncio
async def test_serve_passes_history_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XANA_HOME", str(tmp_path))
    captured: dict = {}

    async def fake_boot(*, project_root, config_provider=None, history_root=None, **kw):
        captured["history_root"] = history_root
        raise RuntimeError("stop-after-boot")  # 截断，不继续起 adapter

    monkeypatch.setattr(serve, "boot", fake_boot)

    import argparse
    argv = argparse.Namespace(project=tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "README_ai.md").write_text("# m\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="stop-after-boot"):
        await serve._run_inner(argv)

    assert captured["history_root"] == tmp_path / "sessions"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m pytest tests/test_serve_history_wiring.py -v`
Expected: FAIL（`captured["history_root"]` 为 None，断言失败）

- [ ] **Step 3: 改 serve.py**

`src/xana/cli/serve.py` 的 `_run_inner` 里（现 194 行）：

```python
    boot_result = await boot(project_root=project_root, config_provider=cp)
```

改为：

```python
    boot_result = await boot(
        project_root=project_root,
        config_provider=cp,
        history_root=_xana_home() / "sessions",
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m pytest tests/test_serve_history_wiring.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/xana/cli/serve.py
git commit -m "feat(serve): 会话历史落盘到 ~/.xana/sessions，重启自动恢复

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
( cd tests && git add test_serve_history_wiring.py && git commit -m "test: serve 传入 history_root" )
```

---

### Task 6: xanac 交互式会话持久化

**Files:**
- Modify: `src/xana/cli/xanac.py`
- Test: 手动烟测（xanac 入口测试成本高，本 Task 用烟测验证，不写自动化测试）

要点：xanac 启动调 `boot(...)` 处补 `history_root=<xana_home>/"sessions"`，让交互式 CLI 会话也落盘 + 下次启动 replay。先读 `xanac.py` 找到 `boot(` 调用与它现有的 home/项目解析方式，复用同一个 `~/.xana/sessions` 路径（与 serve 一致）。

- [ ] **Step 1: 定位 boot 调用**

Run: `grep -n "boot(\|XANA_HOME\|\.xana\|home" src/xana/cli/xanac.py`
Expected: 找到 `boot(project_root=...)` 调用行与 home 解析。

- [ ] **Step 2: 改 xanac.py**

在 xanac 的 `boot(project_root=..., ...)` 调用补 `history_root=<home>/"sessions"`（`<home>` 用 xanac 既有的 xana_home 解析；若 xanac 没有，import `from xana.cli.serve import _xana_home` 并用 `_xana_home() / "sessions"`，与 serve 同源）。

- [ ] **Step 3: 烟测——持久化**

```bash
# 第一次：问一句
PYTHONPATH=src python -m xana.cli.xanac --session smoke-persist "记住数字 42"
# 第二次：新进程，应能 replay 到上一轮历史（看回复是否延续上下文）
PYTHONPATH=src python -m xana.cli.xanac --session smoke-persist "我刚让你记的数字是多少"
# 验证落盘文件存在
ls -l ~/.xana/sessions/smoke-persist.jsonl
```
Expected: `smoke-persist.jsonl` 存在且含两轮消息；第二次进程能看到第一轮上下文。

> 注：若 xanac 是「一次性命令」模式（非常驻 REPL），跨进程 replay 即上面行为；若是 REPL 模式，单进程内本就有上下文，持久化价值在重启后。两种都由本改动覆盖。

- [ ] **Step 4: 跑全量回归**

Run: `PYTHONPATH=src python -m pytest tests/ -q`
Expected: 全绿（除既知 `tests/test_xanac_e2e.py::test_xanac_empty_input_exits_nonzero` 旧失败外）。

- [ ] **Step 5: 提交**

```bash
git add src/xana/cli/xanac.py
git commit -m "feat(xanac): 交互式会话历史落盘 ~/.xana/sessions，重启恢复

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 收尾

- [ ] 跑全量：`PYTHONPATH=src python -m pytest tests/ -q`（绿，除既知 e2e 旧失败）。
- [ ] `~/.xana/bin/xana sync`（同步并自动重启 serve）。
- [ ] 确认主仓与 `tests/` 独立仓都已提交（`git -C . log --oneline -6` + `git -C tests log --oneline -6`）。
- [ ] Phase 1 完成后，写 Phase 2 plan 时读：本 ROADMAP + `ASYNC_RUN_DESIGN.md` +**已 commit 的** `src/xana/memory/file_history.py` 当前形态。

---

## Self-Review（plan 作者已核对）

**1. Spec 覆盖（对照 ROADMAP Phase 1 范围）：**
- FileHistoryMirror 追加写 + 读回 → Task 1 ✅
- 真 MemoryReplayer 重建 ActiveContext → Task 2 ✅
- 目录布局 `{root}/{sid}.jsonl` → Task 1（`_path`）✅
- boot 注入 File 实现（InMemory/File 并存，boot 选择）→ Task 4 ✅
- 启动 replay 恢复 + 中断检测注入 Continue → Task 3 + Task 4 ✅
- serve 用 File → Task 5 ✅；xanac 持久化 → Task 6 ✅
- JSONL 忠实 round-trip MemoryMessage（含 metadata/thinking）→ Task 1 测试覆盖 `reasoning_content` ✅

**2. Placeholder 扫描：** 无 TBD / “处理边界情况”空话；每个改代码步骤都贴了完整代码与精确行锚点。

**3. 类型一致性：** `FileHistoryMirror`/`FileMemoryReplayer`/`needs_continue`/`replay_and_detect_interrupts`/`CONTINUE_TEXT` 在 Task 1-3 定义，Task 4-6 引用同名；`read_rows()` 全程 async；`history_root` 参数名在 boot/serve/test 一致；`ReplayResult` 字段名与 contracts 一致。

**4. 范围纪律：** 无接口（ABC）改动；不引入 Placeholder / write queue / async（留给 Phase 2+）。
