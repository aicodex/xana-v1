# Phase 5 — Cancel / Resume / 重启恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 xana 异步 run 支持:turn 级 cancel(只读→立即 abort、写入→2s 宽限超时强杀)、`/resume` 列表与续跑、重启时扫 `runs/` 恢复未完成、graceful SIGTERM。

**Architecture:**
- `RunRecord` 加 `exit_reason: ExitReason | None`(P5.4 枚举),`RunStore.list_unfinished` 按枚举过滤。
- `tool/_dispatch_tool` 内嵌 abort 拦截:read-only 立即 cancel task;non-read-only `asyncio.wait_for(task, 2.0)`,超时则 cancel + 注入中文 synthetic warning 到 tool_result。
- `runs/{run_id}.jsonl` 在 `/resume` 触发 fork 新 run 时改名 `.aborted.jsonl`;启动扫盘 A(有 `run_completed` marker)赢 B(没 marker = 跑一半)。
- `/resume` 命令:无参列出过滤后的 RunRecord,带参 fork 新 run。
- `cli/xanac.py` + `cli/serve.py` 注册 SIGTERM 处理:abort 所有 background runs,await drain ≤2s,超时则 process 退出(下次启动按 `shutdown_force` 进 /resume)。
- compact 卡 stop 按钮拿掉;`/compact_cancel` 命令保留(键盘可用),按钮不再渲染。

**Tech Stack:** Python 3.12+ / asyncio / dataclasses / 现有 `state` / `harness_sdk` / `agent_adapter` 模块。

**上游 spec:** `docs/superpowers/specs/2026-06-14-p5-p6-decisions.md` §P5。**与之冲突时以 spec 为准。**

**前置条件(必须):** P1–P3 已 commit;P4 PH-first 双卡已上线(commit `df4bda3`);`async_mode` 默认 True;`xana sync` 已跑过、`xana serve` 在新代码上运行。

---

## File Structure

**修改:**
- `src/xana/state/contracts.py` — 加 `ExitReason` Literal + `RunRecord.exit_reason` 字段。
- `src/xana/state/interfaces.py` — `RunStore.list_unfinished` docstring 补 P5.4 过滤语义。
- `src/xana/state/in_memory.py` — `InMemoryRunStore.list_unfinished` 实现过滤。
- `src/xana/state/file_runs.py` — `FileRunStore.list_unfinished` 实现过滤;`RunJsonlWriter` 加 `rename_to_aborted(run_id)` 方法;读 marker 判 A/B。
- `src/xana/tools/contracts.py` — `ToolSpec.read_only` 字段加注释(取消语义)。
- `src/xana/harness_sdk/default.py` — `_dispatch_tool` 包 abort-aware wrapper;`exit_reason` 写入 `RunRecord` 收尾路径。
- `src/xana/agent_adapter/commands.py` — `DefaultCommandRouter` 加 `/resume` handler。
- `src/xana/agent_adapter/gateway.py` — `cancel` 路径标 `exit_reason="user_cancel"`;新加 `resume(session_id, run_id)` 方法。
- `src/xana/cli/xanac.py` — 注册 `SIGTERM` handler;`_repl_loop` 接 graceful shutdown。
- `src/xana/cli/serve.py` — 同上;serve 进程对 SIGTERM 调 graceful shutdown。
- `src/xana/agent_adapter/channels/feishu_card.py` — `build_compact_card` 去掉 stop 按钮分支。
- `src/xana/agent_adapter/channels/telegram_html.py` — 同上,telegram compact 消息无 stop 按钮。

**新增:**
- `src/xana/state/startup_scan.py` — 启动扫 `runs/` 模块,负责 A-赢-B 仲裁、completed-PH 替换、未完成入 /resume 候选。

**新增测试(`tests/` 子 repo):**
- `tests/test_run_exit_reason_enum.py`
- `tests/test_list_unfinished_filter.py`
- `tests/test_tool_spec_read_only_doc.py`
- `tests/test_dispatch_tool_cancel_readonly.py`
- `tests/test_dispatch_tool_cancel_write_2s_grace.py`
- `tests/test_dispatch_tool_force_kill_warning.py`
- `tests/test_run_jsonl_rename_to_aborted.py`
- `tests/test_resume_command_list.py`
- `tests/test_resume_command_fork.py`
- `tests/test_startup_scan_a_wins_over_b.py`
- `tests/test_startup_scan_completed_replaces_ph.py`
- `tests/test_startup_scan_b_enters_resume_list.py`
- `tests/test_sigterm_graceful_drain.py`
- `tests/test_compact_card_no_stop_button.py`

---

## Task 1 — `ExitReason` 枚举 + `RunRecord.exit_reason` 字段

**Files:**
- Modify: `src/xana/state/contracts.py`
- Test: `tests/test_run_exit_reason_enum.py`

### Step 1.1 — 写测试

```python
# tests/test_run_exit_reason_enum.py
"""RunRecord.exit_reason 字段存在,接受 P5.4 枚举值;未设置时为 None。"""
from __future__ import annotations

import pytest

from xana.state.contracts import RunRecord, RunStatus, ExitReason


def test_exit_reason_default_is_none():
    rec = RunRecord(
        run_id="r1", session_id="s1", agent_name="chat", status="running"
    )
    assert rec.exit_reason is None


@pytest.mark.parametrize("reason", [
    "user_cancel", "tool_kill_timeout", "shutdown_graceful", "shutdown_force",
    "network_error", "llm_timeout", "session_lost", "tool_error", "unknown",
])
def test_exit_reason_accepts_all_p54_values(reason: ExitReason):
    rec = RunRecord(
        run_id="r1", session_id="s1", agent_name="chat",
        status="cancelled", exit_reason=reason,
    )
    assert rec.exit_reason == reason
```

- [ ] **Step 1.1: 写测试文件**

- [ ] **Step 1.2: 跑测试确认失败**

Run: `pytest tests/test_run_exit_reason_enum.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExitReason'` 或 `RunRecord` 没 `exit_reason` 字段。

- [ ] **Step 1.3: 加 `ExitReason` Literal 和字段**

在 `src/xana/state/contracts.py` 文件顶部 `Literal` import 附近、`RunStatus` 旁加:

```python
ExitReason = Literal[
    "user_cancel",
    "tool_kill_timeout",
    "shutdown_graceful",
    "shutdown_force",
    "network_error",
    "llm_timeout",
    "session_lost",
    "tool_error",
    "unknown",
]
```

在 `RunRecord` dataclass(当前 57-70 行)末尾(`metadata` 字段之前)插入:

```python
    exit_reason: ExitReason | None = None
```

- [ ] **Step 1.4: 测试通过**

Run: `pytest tests/test_run_exit_reason_enum.py -v`
Expected: 10 PASS(1 default + 9 parametrize)。

- [ ] **Step 1.5: Commit**

```bash
git add src/xana/state/contracts.py
git commit -m "feat(state): add ExitReason enum + RunRecord.exit_reason field (P5.4)"
cd tests && git add test_run_exit_reason_enum.py && \
  git commit -m "test(state): RunRecord.exit_reason field + enum values" && cd ..
```

---

## Task 2 — `RunStore.list_unfinished` 按 `exit_reason` 过滤

**Files:**
- Modify: `src/xana/state/interfaces.py`(docstring 补语义)
- Modify: `src/xana/state/in_memory.py`(`InMemoryRunStore.list_unfinished`)
- Modify: `src/xana/state/file_runs.py`(`FileRunStore.list_unfinished`)
- Test: `tests/test_list_unfinished_filter.py`

### Step 2.1 — 写测试

```python
# tests/test_list_unfinished_filter.py
"""list_unfinished 排除 P5.4 黑名单 exit_reason(network_error 等);
status running/paused 但 exit_reason 是黑名单值时也不出现。"""
from __future__ import annotations

import pytest

from xana.state.contracts import RunRecord
from xana.state.in_memory import InMemoryRunStore


@pytest.mark.asyncio
async def test_list_unfinished_excludes_blacklisted_exit_reasons():
    store = InMemoryRunStore()
    # 白名单进
    await store.create(RunRecord(run_id="ok1", session_id="s", agent_name="c",
                                 status="cancelled", exit_reason="user_cancel"))
    await store.create(RunRecord(run_id="ok2", session_id="s", agent_name="c",
                                 status="cancelled", exit_reason="tool_kill_timeout"))
    await store.create(RunRecord(run_id="ok3", session_id="s", agent_name="c",
                                 status="paused", exit_reason="shutdown_graceful"))
    await store.create(RunRecord(run_id="ok4", session_id="s", agent_name="c",
                                 status="paused", exit_reason="shutdown_force"))
    # 黑名单出
    for rid, reason in [
        ("no1", "network_error"), ("no2", "llm_timeout"),
        ("no3", "session_lost"), ("no4", "tool_error"), ("no5", "unknown"),
    ]:
        await store.create(RunRecord(
            run_id=rid, session_id="s", agent_name="c",
            status="cancelled", exit_reason=reason,
        ))
    # 仍在跑的(无 exit_reason)进
    await store.create(RunRecord(run_id="running1", session_id="s", agent_name="c",
                                 status="running"))

    unfinished = await store.list_unfinished()
    ids = {r.run_id for r in unfinished}
    assert ids == {"ok1", "ok2", "ok3", "ok4", "running1"}
```

- [ ] **Step 2.1: 写测试**

- [ ] **Step 2.2: 跑测试确认失败**

Run: `pytest tests/test_list_unfinished_filter.py -v`
Expected: FAIL — 当前实现按 `status in ("running","paused")` 过滤,会漏放 ok1/ok2(cancelled),也会放 no1-no5(若 status 是 running/paused)。

- [ ] **Step 2.3: 在 `state/interfaces.py` 补 docstring**

找到 `RunStore` ABC 里 `list_unfinished` 方法定义,把 docstring 改为:

```python
async def list_unfinished(self) -> tuple[RunRecord, ...]:
    """返回可恢复(/resume 候选)的 run 记录。

    过滤规则(P5.4):
      - status 在 {"running","paused"} **或** exit_reason 在
        {"user_cancel","tool_kill_timeout","shutdown_graceful","shutdown_force"}
        的记录进入候选。
      - exit_reason 在
        {"network_error","llm_timeout","session_lost","tool_error","unknown"}
        的一律排除(无论 status)。
    """
    ...
```

- [ ] **Step 2.4: `InMemoryRunStore.list_unfinished` 实现过滤**

把 `state/in_memory.py` 里 `InMemoryRunStore.list_unfinished` 的过滤替换为:

```python
async def list_unfinished(self) -> tuple[RunRecord, ...]:
    _RESUMABLE_EXIT = {
        "user_cancel", "tool_kill_timeout",
        "shutdown_graceful", "shutdown_force",
    }
    _EXCLUDED_EXIT = {
        "network_error", "llm_timeout",
        "session_lost", "tool_error", "unknown",
    }
    out: list[RunRecord] = []
    for rec in self._records.values():  # 字段名以实现为准;若是 _runs 改名
        if rec.exit_reason in _EXCLUDED_EXIT:
            continue
        if rec.status in ("running", "paused") or rec.exit_reason in _RESUMABLE_EXIT:
            out.append(rec)
    return tuple(out)
```

(若 `InMemoryRunStore` 内部字典名不是 `_records`,看实际文件用对应字段名。)

- [ ] **Step 2.5: `FileRunStore.list_unfinished` 同步实现**

在 `state/file_runs.py`(约 114-115 行)把过滤替换为相同两组常量 + 同样判定逻辑(读所有 `{root}/{run_id}.json` 文件,反序列化 → 同样过滤)。常量可以在 `state/contracts.py` 末尾导出共享:

```python
# state/contracts.py 末尾追加(供 store 实现复用):
RESUMABLE_EXIT_REASONS: frozenset[ExitReason] = frozenset({
    "user_cancel", "tool_kill_timeout",
    "shutdown_graceful", "shutdown_force",
})
EXCLUDED_EXIT_REASONS: frozenset[ExitReason] = frozenset({
    "network_error", "llm_timeout",
    "session_lost", "tool_error", "unknown",
})
```

`in_memory.py` / `file_runs.py` 改 import 这两个常量,避免重复硬编码。

- [ ] **Step 2.6: 测试通过 + 顺带跑相关回归**

Run: `pytest tests/test_list_unfinished_filter.py tests/test_run_exit_reason_enum.py -v`
Expected: ALL PASS。

- [ ] **Step 2.7: Commit**

```bash
git add src/xana/state/contracts.py src/xana/state/interfaces.py \
        src/xana/state/in_memory.py src/xana/state/file_runs.py
git commit -m "feat(state): list_unfinished filters by ExitReason per P5.4"
cd tests && git add test_list_unfinished_filter.py && \
  git commit -m "test(state): list_unfinished excludes blacklisted exit reasons" && cd ..
```

---

## Task 3 — `ToolSpec.read_only` 注释取消语义

**Files:**
- Modify: `src/xana/tools/contracts.py`
- Test: `tests/test_tool_spec_read_only_doc.py`

### Step 3.1 — 写测试

```python
# tests/test_tool_spec_read_only_doc.py
"""ToolSpec.read_only 的注释明确取消语义 — 用 ast 检查源码注释中关键词存在。"""
from __future__ import annotations

import inspect
import xana.tools.contracts as contracts_mod


def test_read_only_docstring_mentions_cancel_semantics():
    src = inspect.getsource(contracts_mod.ToolSpec)
    # 注释里必须出现"取消"和"2 秒"两个关键词,说明语义已记录
    assert "取消" in src
    assert "2" in src and ("秒" in src or "s 宽限" in src or "second" in src)
```

- [ ] **Step 3.1: 写测试**

- [ ] **Step 3.2: 跑测试确认失败**

Run: `pytest tests/test_tool_spec_read_only_doc.py -v`
Expected: FAIL — 当前 `read_only` 无注释。

- [ ] **Step 3.3: 加注释**

`src/xana/tools/contracts.py` 里 `ToolSpec.read_only` 字段(约 50-65 行附近)上方紧邻位置加注释:

```python
    # `read_only` 也用于 cancel 语义(P5.1):
    #   True  → cancel 时立即 abort tool,resume = 重跑该工具
    #   False → cancel 时等 2 秒宽限,完成则正常写,超时强杀 + 注入
    #           synthetic warning(P5.2),resume = fork 新 run
    read_only: bool = False
```

- [ ] **Step 3.4: 测试通过 + commit**

```bash
git add src/xana/tools/contracts.py
git commit -m "docs(tools): ToolSpec.read_only annotated with cancel semantics (P5.1)"
cd tests && git add test_tool_spec_read_only_doc.py && \
  git commit -m "test(tools): read_only doc mentions cancel + 2s grace" && cd ..
```

---

## Task 4 — `_dispatch_tool` 内嵌 abort 拦截:read-only 立即 cancel

**Files:**
- Modify: `src/xana/harness_sdk/default.py`(`_dispatch_tool` 234-255 区段)
- Test: `tests/test_dispatch_tool_cancel_readonly.py`

### Step 4.1 — 写测试

```python
# tests/test_dispatch_tool_cancel_readonly.py
"""abort 到来时,read_only 工具立即 cancel,tool_result.failure_kind='cancelled'。"""
from __future__ import annotations

import asyncio
import pytest

from xana.harness_sdk.contracts import AbortSignal
from xana.tools.contracts import ToolSpec, ToolResult


class _HangingReadOnlyTool:
    spec = ToolSpec(name="read_hang", read_only=True)
    async def validate(self, inv): return None
    async def check_permission(self, inv, ctx):
        from xana.policy.contracts import PermissionDecision
        return PermissionDecision(decision="allow", reason="test")
    async def pre_tool_use(self, inv, ctx): return inv
    async def execute(self, prepared, ctx):
        await asyncio.sleep(30.0)  # hang
        return ToolResult(ok=True, content="never")
    async def post_tool_use(self, prepared, ctx, result): return result


@pytest.mark.asyncio
async def test_readonly_tool_cancels_immediately_on_abort():
    from xana.harness_sdk.default import DefaultHarness  # type: ignore
    # 构造最小 HarnessContext 调 _dispatch_tool(签名按实际)
    abort = AbortSignal(aborted=False, reason=None)
    harness = DefaultHarness()  # 实际构造按现有 boot 风格
    # 启动 dispatch + 0.05s 后翻 abort.aborted
    async def trip():
        await asyncio.sleep(0.05)
        # 通过 harness 内部的 abort 引用翻位
        harness._abort_signal.aborted = True  # 占位字段名;按实际改
        harness._abort_signal.reason = "user_cancel"
    asyncio.create_task(trip())

    t0 = asyncio.get_event_loop().time()
    result = await harness._dispatch_tool_for_test(_HangingReadOnlyTool())
    elapsed = asyncio.get_event_loop().time() - t0

    assert elapsed < 1.0, f"read_only 应立即取消,实际 {elapsed:.2f}s"
    assert result.ok is False
    assert result.failure_kind == "cancelled"
```

> ⚠️ 测试假设 `DefaultHarness` 暴露一个 `_dispatch_tool_for_test(tool)` 测试钩子;实际应在实现期对照 `harness_sdk/default.py:234-255` 的 `_dispatch_tool` 签名构造完整调用(`tc`/`context`/`session_id`/...)。若构造复杂,提取一个内部小 helper `_run_tool_with_abort(execute_coro, abort, read_only) -> ToolResult` 单独测试。**实际实现见 Step 4.3**。

- [ ] **Step 4.1: 写测试**

- [ ] **Step 4.2: 跑测试确认失败**

Run: `pytest tests/test_dispatch_tool_cancel_readonly.py -v`
Expected: FAIL — `_dispatch_tool_for_test` 不存在,或 abort 不会取消 hang 中的 tool。

- [ ] **Step 4.3: 抽出 `_run_tool_with_abort` helper 并嵌入 `_dispatch_tool`**

在 `harness_sdk/default.py` 文件加(`DefaultHarness` 类内部):

```python
async def _run_tool_with_abort(
    self,
    execute_coro_factory,         # callable returning awaitable
    abort,                        # AbortSignal
    read_only: bool,
):
    """abort 触发时:
       - read_only=True → 立即取消;返回 cancelled ToolResult
       - read_only=False → 等 2.0s 宽限;超时强杀 + 注入 synthetic warning
    """
    from xana.tools.contracts import ToolResult
    task = asyncio.create_task(execute_coro_factory())
    poll_interval = 0.05
    GRACE_SECONDS = 2.0

    while not task.done():
        await asyncio.sleep(poll_interval)
        if abort.aborted:
            if read_only:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return ToolResult(
                    ok=False,
                    content=_SYNTHETIC_WARNING_TEXT_READONLY,
                    failure_kind="cancelled",
                    error="abort during read-only tool",
                )
            # 写入工具:等 2s 宽限
            try:
                result = await asyncio.wait_for(asyncio.shield(task), GRACE_SECONDS)
                return result  # 完成了就正常写
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return ToolResult(
                    ok=False,
                    content=_SYNTHETIC_WARNING_TEXT_KILLED,
                    failure_kind="cancelled",
                    error="abort + 2s grace timeout",
                )
    return task.result()
```

在文件顶部加常量(`P5.2` 决议中文文案):

```python
_SYNTHETIC_WARNING_TEXT_KILLED = (
    "[工具执行已被用户取消(超过 2 秒宽限期后强制终止)。该操作可能已部分完成 "
    "—— 切勿自动重试,半完成状态下重试可能让目标变得更糟。请等待用户的下一步指示。]"
)
_SYNTHETIC_WARNING_TEXT_READONLY = (
    "[只读工具被用户取消。该工具未产生副作用,可由用户决定是否在下一轮重试。]"
)
```

在 `_dispatch_tool` 中调用 `tool.execute(prepared, tool_ctx)` 那一行(默认 default.py:234-255 区段)替换为:

```python
result = await self._run_tool_with_abort(
    lambda: tool.execute(prepared, tool_ctx),
    abort=context.abort,
    read_only=tool.spec.read_only,
)
```

并 import:

```python
import asyncio
import contextlib
```

(若已有则跳过。)

- [ ] **Step 4.4: 测试通过 + commit**

Run: `pytest tests/test_dispatch_tool_cancel_readonly.py -v`
Expected: PASS。

```bash
git add src/xana/harness_sdk/default.py
git commit -m "feat(harness): _run_tool_with_abort — read_only cancels immediately (P5.1)"
cd tests && git add test_dispatch_tool_cancel_readonly.py && \
  git commit -m "test(harness): read_only tool aborts immediately on cancel" && cd ..
```

---

## Task 5 — 写入工具 2s 宽限 + 完成走正常写

**Files:**
- Test: `tests/test_dispatch_tool_cancel_write_2s_grace.py`
- 实现已在 Task 4 `_run_tool_with_abort` 完成;本任务只是测覆盖另一分支。

### Step 5.1 — 写测试

```python
# tests/test_dispatch_tool_cancel_write_2s_grace.py
"""非 read_only 工具被 abort 时,若在 2s 内完成,正常写;不注入 warning。"""
from __future__ import annotations

import asyncio
import pytest

from xana.harness_sdk.contracts import AbortSignal
from xana.tools.contracts import ToolSpec, ToolResult


class _WriteToolFinishesIn500ms:
    spec = ToolSpec(name="w_quick", read_only=False)
    async def execute(self, prepared, ctx):
        await asyncio.sleep(0.5)
        return ToolResult(ok=True, content="written")


@pytest.mark.asyncio
async def test_write_tool_finishing_within_grace_writes_normally():
    from xana.harness_sdk.default import DefaultHarness
    h = DefaultHarness()
    h._abort_signal = AbortSignal(aborted=False, reason=None)

    async def trip():
        await asyncio.sleep(0.05)
        h._abort_signal.aborted = True
        h._abort_signal.reason = "user_cancel"
    asyncio.create_task(trip())

    result = await h._run_tool_with_abort(
        lambda: _WriteToolFinishesIn500ms().execute(None, None),
        abort=h._abort_signal,
        read_only=False,
    )
    assert result.ok is True
    assert result.content == "written"
    assert result.failure_kind is None
```

- [ ] **Step 5.1: 写测试 + 跑测试确认通过**

Run: `pytest tests/test_dispatch_tool_cancel_write_2s_grace.py -v`
Expected: PASS(实现已在 Task 4 完成)。

- [ ] **Step 5.2: Commit**

```bash
cd tests && git add test_dispatch_tool_cancel_write_2s_grace.py && \
  git commit -m "test(harness): write tool finishing within 2s grace writes normally" && cd ..
```

---

## Task 6 — 写入工具 2s 超时强杀 + 注入 synthetic warning

**Files:**
- Test: `tests/test_dispatch_tool_force_kill_warning.py`
- 实现 Task 4 已完成。

### Step 6.1 — 写测试

```python
# tests/test_dispatch_tool_force_kill_warning.py
"""写入工具 hang 超 2s → 强杀;tool_result.content 含 P5.2 中文 warning。"""
from __future__ import annotations

import asyncio
import pytest

from xana.harness_sdk.contracts import AbortSignal
from xana.tools.contracts import ToolSpec, ToolResult


class _HangingWriteTool:
    spec = ToolSpec(name="w_hang", read_only=False)
    async def execute(self, prepared, ctx):
        await asyncio.sleep(30.0)
        return ToolResult(ok=True, content="never")


@pytest.mark.asyncio
async def test_write_tool_force_killed_injects_warning_after_2s():
    from xana.harness_sdk.default import DefaultHarness
    h = DefaultHarness()
    h._abort_signal = AbortSignal(aborted=False, reason=None)

    async def trip():
        await asyncio.sleep(0.05)
        h._abort_signal.aborted = True
    asyncio.create_task(trip())

    t0 = asyncio.get_event_loop().time()
    result = await h._run_tool_with_abort(
        lambda: _HangingWriteTool().execute(None, None),
        abort=h._abort_signal,
        read_only=False,
    )
    elapsed = asyncio.get_event_loop().time() - t0
    assert 1.8 < elapsed < 3.5, f"应等约 2s,实际 {elapsed:.2f}s"
    assert result.ok is False
    assert result.failure_kind == "cancelled"
    assert "强制终止" in result.content
    assert "切勿自动重试" in result.content
```

- [ ] **Step 6.1: 写测试 + 跑确认通过**

Run: `pytest tests/test_dispatch_tool_force_kill_warning.py -v`
Expected: PASS。

- [ ] **Step 6.2: Commit**

```bash
cd tests && git add test_dispatch_tool_force_kill_warning.py && \
  git commit -m "test(harness): write tool kill after 2s injects synthetic warning" && cd ..
```

---

## Task 7 — `RunJsonlWriter.rename_to_aborted`

**Files:**
- Modify: `src/xana/state/file_runs.py`(`RunJsonlWriter`,约 124-144 行)
- Test: `tests/test_run_jsonl_rename_to_aborted.py`

### Step 7.1 — 写测试

```python
# tests/test_run_jsonl_rename_to_aborted.py
"""rename_to_aborted 把 runs/{run_id}.jsonl 改名 runs/{run_id}.aborted.jsonl。"""
from __future__ import annotations

import pathlib
import pytest

from xana.state.file_runs import RunJsonlWriter


def test_rename_to_aborted(tmp_path: pathlib.Path):
    runs_root = tmp_path / "session1" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "r1.jsonl").write_text('{"foo":"bar"}\n')
    w = RunJsonlWriter(sessions_root=tmp_path / "session1")
    w.rename_to_aborted("r1")
    assert (runs_root / "r1.aborted.jsonl").exists()
    assert not (runs_root / "r1.jsonl").exists()


def test_rename_to_aborted_noop_if_missing(tmp_path: pathlib.Path):
    runs_root = tmp_path / "session1" / "runs"
    runs_root.mkdir(parents=True)
    w = RunJsonlWriter(sessions_root=tmp_path / "session1")
    # 不存在时静默 noop
    w.rename_to_aborted("does-not-exist")
    assert not (runs_root / "does-not-exist.aborted.jsonl").exists()
```

- [ ] **Step 7.1: 写测试 + 跑确认失败**

Run: `pytest tests/test_run_jsonl_rename_to_aborted.py -v`
Expected: FAIL — `RunJsonlWriter.rename_to_aborted` 不存在。

- [ ] **Step 7.2: 实现方法**

在 `state/file_runs.py` 的 `RunJsonlWriter` 类(124-144 行)加方法:

```python
def rename_to_aborted(self, run_id: str) -> None:
    """把 runs/{run_id}.jsonl 改名为 runs/{run_id}.aborted.jsonl(P5.3)。

    文件不存在时静默 noop —— resume 路径在已 aborted 的 run 上不应失败。
    """
    import pathlib
    src = pathlib.Path(self._runs_root) / f"{run_id}.jsonl"
    if not src.exists():
        return
    dst = pathlib.Path(self._runs_root) / f"{run_id}.aborted.jsonl"
    src.rename(dst)
```

(`self._runs_root` 是实际字段名,以现有实现为准;若是 `self._dir` 则按实际改。)

- [ ] **Step 7.3: 测试通过 + commit**

```bash
git add src/xana/state/file_runs.py
git commit -m "feat(state): RunJsonlWriter.rename_to_aborted (P5.3)"
cd tests && git add test_run_jsonl_rename_to_aborted.py && \
  git commit -m "test(state): rename_to_aborted moves jsonl to .aborted.jsonl" && cd ..
```

---

## Task 8 — `/resume` 命令:列表 + fork

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`(`DefaultCommandRouter` 加 spec + handler)
- Modify: `src/xana/agent_adapter/gateway.py`(加 `resume(session_id, run_id)` 方法)
- Test: `tests/test_resume_command_list.py`, `tests/test_resume_command_fork.py`

### Step 8.1 — 写列表测试

```python
# tests/test_resume_command_list.py
"""/resume(无参)调 list_unfinished,把结果格式化成消息。"""
from __future__ import annotations

import pytest

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.commands import DefaultCommandRouter
from xana.state.in_memory import InMemoryRunStore
from xana.state.contracts import RunRecord


class _GatewayStub:
    def __init__(self, store): self.run_store = store
    async def cancel(self, sid, rid): pass


@pytest.mark.asyncio
async def test_resume_no_args_lists_unfinished_runs():
    store = InMemoryRunStore()
    await store.create(RunRecord(
        run_id="r1", session_id="s1", agent_name="chat",
        status="cancelled", exit_reason="user_cancel",
        input_request={"text": "review yuxu"},
    ))
    await store.create(RunRecord(
        run_id="r2", session_id="s1", agent_name="chat",
        status="cancelled", exit_reason="network_error",  # 黑名单
    ))
    gw = _GatewayStub(store)
    router = DefaultCommandRouter(gateway=gw)
    result = await router.dispatch(InboundMessage(
        adapter_id="cli", session_id="s1", text="/resume"
    ))
    assert result.ok
    assert "r1" in result.message
    assert "review yuxu" in result.message
    assert "r2" not in result.message
```

- [ ] **Step 8.1: 写列表测试 + 跑确认失败**

Run: `pytest tests/test_resume_command_list.py -v`
Expected: FAIL — `/resume` 路由不存在。

- [ ] **Step 8.2: 注册 spec + handler 框架**

`agent_adapter/commands.py` 在 `self._specs` 注册块(约 80-96 行)加:

```python
"/resume": CommandSpec(
    name="/resume",
    description="列出可恢复的后台 run,或 /resume <run_id> 续跑",
    triggers_turn=False,
),
```

在 `self._handlers` 加:

```python
"/resume": DefaultCommandRouter._cmd_resume,
```

加方法:

```python
async def _cmd_resume(self, inbound: "InboundMessage", args: str) -> "CommandResult":
    if self.gateway is None or getattr(self.gateway, "run_store", None) is None:
        return CommandResult(ok=False, message="gateway/run_store 未注入")
    rid = (args or "").strip()
    if not rid:
        # 列表模式 —— /resume 无参
        records = await self.gateway.run_store.list_unfinished()
        # 仅本 session
        recs = [r for r in records if r.session_id == inbound.session_id]
        if not recs:
            return CommandResult(ok=True, message="无可恢复的 run")
        lines = ["可恢复的 run:"]
        for r in recs:
            label = (r.input_request.get("text") or "")[:40] or "(无 label)"
            lines.append(f"  {r.run_id}  · {r.exit_reason or r.status}  · {label}")
        lines.append("\n用 /resume <run_id> 续跑")
        return CommandResult(ok=True, message="\n".join(lines))
    # 续跑模式
    await self.gateway.resume(inbound.session_id, rid)
    return CommandResult(ok=True, message=f"[resume] 续跑 {rid}")
```

- [ ] **Step 8.3: gateway.resume 占位 + 防重入**

在 `agent_adapter/gateway.py` 加:

```python
async def resume(self, session_id: str, run_id: str) -> None:
    """续跑 = (1) 把原 run JSONL 改名 .aborted.jsonl
            (2) fork 新 run 跑 — 用相同 user input。

    防重入:同一 run_id 同时只能 resume 一次(用 lock 或 in-flight 集合)。
    """
    if not hasattr(self, "_resume_inflight"):
        self._resume_inflight: set[str] = set()
    if run_id in self._resume_inflight:
        return
    self._resume_inflight.add(run_id)
    try:
        rec = await self.run_store.get(run_id)
        if rec is None:
            return
        # (1) 改名 — 实际 jsonl_writer 注入位置按 boot 实际
        writer = getattr(self, "run_jsonl_writer", None)
        if writer is not None:
            writer.rename_to_aborted(run_id)
        # (2) 用原 input_request 重新 submit
        text = rec.input_request.get("text", "")
        await self._submit_internal(  # 现有 submit 私有入口
            session_id=session_id, text=text, adapter_id="cli",
        )
    finally:
        self._resume_inflight.discard(run_id)
```

(`_submit_internal` 是占位名;实际看 gateway 现有 `_submit`/`submit` 方法签名,从 `inbound` 构造再调。)

- [ ] **Step 8.4: 列表测试通过**

Run: `pytest tests/test_resume_command_list.py -v`
Expected: PASS。

### Step 8.5 — 写 fork 测试

```python
# tests/test_resume_command_fork.py
"""/resume <run_id> 调 gateway.resume,触发 jsonl 改名 + 新 submit。"""
from __future__ import annotations

import pathlib
import pytest

from xana.agent_adapter.contracts import InboundMessage
from xana.agent_adapter.commands import DefaultCommandRouter


class _SpyGateway:
    def __init__(self):
        self.run_store = None
        self.resumed: list[tuple[str, str]] = []
    async def resume(self, sid, rid):
        self.resumed.append((sid, rid))


@pytest.mark.asyncio
async def test_resume_with_run_id_calls_gateway_resume():
    gw = _SpyGateway()
    router = DefaultCommandRouter(gateway=gw)
    result = await router.dispatch(InboundMessage(
        adapter_id="cli", session_id="s1", text="/resume r-abc-1"
    ))
    assert result.ok
    assert gw.resumed == [("s1", "r-abc-1")]
```

- [ ] **Step 8.5: 写 fork 测试 + 跑确认通过**

Run: `pytest tests/test_resume_command_fork.py -v`
Expected: PASS。

- [ ] **Step 8.6: Commit**

```bash
git add src/xana/agent_adapter/commands.py src/xana/agent_adapter/gateway.py
git commit -m "feat(adapter): /resume command + gateway.resume with reentry guard"
cd tests && git add test_resume_command_list.py test_resume_command_fork.py && \
  git commit -m "test(adapter): /resume list + fork via gateway.resume" && cd ..
```

---

## Task 9 — 启动扫 `runs/` — A 赢 B + completed PH 替换 + B 入 /resume

**Files:**
- Create: `src/xana/state/startup_scan.py`
- Modify: `src/xana/boot.py`(或 boot 入口)调 startup_scan 在 active_context 重建之后
- Test: `tests/test_startup_scan_a_wins_over_b.py`, `tests/test_startup_scan_completed_replaces_ph.py`, `tests/test_startup_scan_b_enters_resume_list.py`

### Step 9.1 — 写 A 赢 B 测试

```python
# tests/test_startup_scan_a_wins_over_b.py
"""扫到同 run_id 既有 run_completed marker 又看似跑一半,A 赢 — 替换 PH。"""
from __future__ import annotations

import json
import pathlib
import pytest

from xana.state.startup_scan import scan_runs_dir


def test_a_wins_when_completed_marker_present(tmp_path: pathlib.Path):
    runs_root = tmp_path / "session1" / "runs"
    runs_root.mkdir(parents=True)
    jsonl = runs_root / "r1.jsonl"
    # 写一条不完整看起来一半的 row + 一个 run_completed marker
    lines = [
        json.dumps({"row_id": "x", "session_id": "s1", "agent_name": "chat",
                    "kind": "assistant", "ts": 1.0, "payload": {"role":"assistant","content":""}}),
        json.dumps({"marker": "run_completed", "status": "completed", "ts": 2.0}),
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    report = scan_runs_dir(runs_root)
    assert "r1" in report.completed_run_ids
    assert "r1" not in report.unfinished_run_ids
```

- [ ] **Step 9.1: 写测试 + 跑确认失败**

Run: `pytest tests/test_startup_scan_a_wins_over_b.py -v`
Expected: FAIL — `state/startup_scan.py` 不存在。

- [ ] **Step 9.2: 创建 `startup_scan.py` 模块**

`src/xana/state/startup_scan.py`:

```python
"""启动扫 runs/ 目录,产 ScanReport — 给 boot 后续处理 PH 替换 + /resume 候选填充。

仲裁规则(P5.5):有 run_completed marker 则 A 赢,不管 JSONL 看起来是否完整。
.aborted.jsonl 后缀文件跳过(已是 resume fork 的源)。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScanReport:
    completed_run_ids: tuple[str, ...] = ()    # 应触发 PH 替换
    unfinished_run_ids: tuple[str, ...] = ()   # 应入 /resume(exit_reason=shutdown_force)
    aborted_run_ids: tuple[str, ...] = ()      # 已是 .aborted.jsonl,跳过


def scan_runs_dir(runs_root: pathlib.Path) -> ScanReport:
    if not runs_root.exists():
        return ScanReport()
    completed: list[str] = []
    unfinished: list[str] = []
    aborted: list[str] = []
    for path in sorted(runs_root.iterdir()):
        name = path.name
        if name.endswith(".aborted.jsonl"):
            aborted.append(name[:-len(".aborted.jsonl")])
            continue
        if not name.endswith(".jsonl"):
            continue
        run_id = name[:-len(".jsonl")]
        has_completed_marker = False
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("marker") == "run_completed":
                        has_completed_marker = True
                        break
        except OSError:
            continue
        if has_completed_marker:
            completed.append(run_id)
        else:
            unfinished.append(run_id)
    return ScanReport(
        completed_run_ids=tuple(completed),
        unfinished_run_ids=tuple(unfinished),
        aborted_run_ids=tuple(aborted),
    )
```

- [ ] **Step 9.3: A 赢 B 测试通过**

Run: `pytest tests/test_startup_scan_a_wins_over_b.py -v`
Expected: PASS。

### Step 9.4 — 写 "completed → 替换 PH" 测试

```python
# tests/test_startup_scan_completed_replaces_ph.py
"""scan_report 标 completed 的 run 应在 PH 替换 hook 后从 main 消失。

测的是 boot 调用顺序:scan_runs_dir → 对每个 completed_run_id 调
active_context_store.replace_placeholder(run_id, jsonl_path)。
"""
from __future__ import annotations

import json
import pathlib
import pytest

from xana.state.startup_scan import scan_runs_dir
from xana.state.startup_scan import replace_completed_placeholders  # 见 Step 9.5


class _FakeStore:
    def __init__(self):
        self.replaced: list[str] = []
    async def replace_placeholder(self, session_id, agent_name, run_id, messages):
        self.replaced.append(run_id)


@pytest.mark.asyncio
async def test_replace_completed_placeholders_invokes_store(tmp_path):
    runs_root = tmp_path / "session1" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "r1.jsonl").write_text(
        json.dumps({"marker": "run_completed", "status": "completed", "ts": 1.0}) + "\n"
    )
    report = scan_runs_dir(runs_root)
    store = _FakeStore()
    await replace_completed_placeholders(
        report=report,
        runs_root=runs_root,
        session_id="session1",
        agent_name="chat",
        store=store,
    )
    assert "r1" in store.replaced
```

- [ ] **Step 9.4: 写测试 + 跑确认失败**

Run: `pytest tests/test_startup_scan_completed_replaces_ph.py -v`
Expected: FAIL — `replace_completed_placeholders` 不存在。

- [ ] **Step 9.5: 加 helper 到 `startup_scan.py`**

在 `state/startup_scan.py` 末尾追加:

```python
async def replace_completed_placeholders(
    *,
    report: ScanReport,
    runs_root: pathlib.Path,
    session_id: str,
    agent_name: str,
    store,  # ActiveContextStore
) -> None:
    """对每个 completed run,从 jsonl 读出 messages 替换 PH。"""
    from xana.memory.runs_loader import load_run_messages
    for rid in report.completed_run_ids:
        jsonl_path = runs_root / f"{rid}.jsonl"
        try:
            messages = load_run_messages(jsonl_path)
        except Exception:
            continue
        await store.replace_placeholder(session_id, agent_name, rid, messages)
```

(若 `xana.memory.runs_loader.load_run_messages` 签名/名称不一致,按 `memory/runs_loader.py` 实际函数名改;survey 报告显示 row 已有 kind/payload 结构。)

- [ ] **Step 9.6: 测试通过**

Run: `pytest tests/test_startup_scan_completed_replaces_ph.py -v`
Expected: PASS。

### Step 9.7 — 写 "unfinished → 入 /resume 列表" 测试

```python
# tests/test_startup_scan_b_enters_resume_list.py
"""未完成扫到 → RunRecord status=paused, exit_reason=shutdown_force 写入 RunStore。"""
from __future__ import annotations

import json
import pathlib
import pytest

from xana.state.in_memory import InMemoryRunStore
from xana.state.startup_scan import scan_runs_dir, mark_unfinished_as_resumable


@pytest.mark.asyncio
async def test_unfinished_marked_shutdown_force(tmp_path):
    runs_root = tmp_path / "session1" / "runs"
    runs_root.mkdir(parents=True)
    # 没 marker 的 jsonl — 看起来是跑一半
    (runs_root / "r1.jsonl").write_text(
        json.dumps({"row_id":"x","session_id":"s1","agent_name":"chat",
                    "kind":"assistant","ts":1.0,"payload":{"role":"assistant","content":""}}) + "\n"
    )
    report = scan_runs_dir(runs_root)
    store = InMemoryRunStore()
    await mark_unfinished_as_resumable(
        report=report, store=store,
        session_id="session1", agent_name="chat",
    )
    unfinished = await store.list_unfinished()
    rec = next(r for r in unfinished if r.run_id == "r1")
    assert rec.exit_reason == "shutdown_force"
```

- [ ] **Step 9.7: 写测试 + 跑确认失败**

Run: `pytest tests/test_startup_scan_b_enters_resume_list.py -v`
Expected: FAIL — `mark_unfinished_as_resumable` 不存在。

- [ ] **Step 9.8: 加 helper**

在 `state/startup_scan.py` 末尾追加:

```python
async def mark_unfinished_as_resumable(
    *,
    report: ScanReport,
    store,  # RunStore
    session_id: str,
    agent_name: str,
) -> None:
    """把扫到的未完成 run 写入 RunStore,exit_reason=shutdown_force,status=paused。
    若 run_id 在 store 已存在,update;否则 create。
    """
    from xana.state.contracts import RunRecord
    for rid in report.unfinished_run_ids:
        existing = await store.get(rid)
        if existing is None:
            await store.create(RunRecord(
                run_id=rid, session_id=session_id, agent_name=agent_name,
                status="paused", exit_reason="shutdown_force",
            ))
        else:
            # 已有记录但状态可能是 running — 同步成 paused + shutdown_force
            updated = RunRecord(
                run_id=existing.run_id, session_id=existing.session_id,
                agent_name=existing.agent_name, status="paused",
                input_request=existing.input_request,
                last_checkpoint=existing.last_checkpoint,
                usage=existing.usage,
                pause_reason=existing.pause_reason,
                abort_reason=existing.abort_reason,
                error=existing.error,
                exit_reason="shutdown_force",
                metadata=existing.metadata,
            )
            await store.update(updated)  # 若是 update_status 改用对应方法
```

- [ ] **Step 9.9: 测试通过 + commit**

Run: `pytest tests/test_startup_scan_b_enters_resume_list.py -v`
Expected: PASS。

```bash
git add src/xana/state/startup_scan.py
git commit -m "feat(state): startup_scan A-wins arbitration + PH replace + resumable mark (P5.5/5.6)"
cd tests && git add test_startup_scan_a_wins_over_b.py \
                  test_startup_scan_completed_replaces_ph.py \
                  test_startup_scan_b_enters_resume_list.py && \
  git commit -m "test(state): startup_scan three branches" && cd ..
```

- [ ] **Step 9.10: 在 boot 接入**

在 `src/xana/boot.py`(或现有 boot 入口)的 `boot()` 末尾、`replay` 完成之后,加调用:

```python
from xana.state.startup_scan import (
    scan_runs_dir, replace_completed_placeholders, mark_unfinished_as_resumable,
)
import pathlib

runs_root = pathlib.Path(session_dir) / "runs"
report = scan_runs_dir(runs_root)
for sid in session_ids:  # 每个 session 一次
    await replace_completed_placeholders(
        report=report, runs_root=runs_root,
        session_id=sid, agent_name="chat", store=active_context_store,
    )
    await mark_unfinished_as_resumable(
        report=report, store=run_store,
        session_id=sid, agent_name="chat",
    )
```

(按实际 boot 函数签名调整;若 boot 只处理单 session 去掉 for 循环。)

- [ ] **Step 9.11: Commit boot 接入**

```bash
git add src/xana/boot.py
git commit -m "feat(boot): wire startup_scan after replay (P5.5)"
```

---

## Task 10 — Graceful SIGTERM:abort 后台 run + 2s drain

**Files:**
- Modify: `src/xana/cli/serve.py`(serve 进程的 SIGTERM 入口)
- Modify: `src/xana/cli/xanac.py`(交互入口同处理)
- Test: `tests/test_sigterm_graceful_drain.py`

### Step 10.1 — 写测试

```python
# tests/test_sigterm_graceful_drain.py
"""graceful_shutdown(timeout=2.0) 翻 abort,等 background runs drain;
超时返回 timed_out=True;按时退出返回 timed_out=False。"""
from __future__ import annotations

import asyncio
import pytest

from xana.cli.shutdown import graceful_shutdown  # 新模块


class _StubGateway:
    def __init__(self, finish_after: float):
        self.finish_after = finish_after
        self.aborted = False
        self.background_tasks: list[asyncio.Task] = []
    async def cancel_all(self):
        self.aborted = True
    def has_background(self):
        return any(not t.done() for t in self.background_tasks)


@pytest.mark.asyncio
async def test_drain_within_grace_returns_false():
    gw = _StubGateway(finish_after=0.5)
    async def fake_run():
        await asyncio.sleep(0.5)
    gw.background_tasks.append(asyncio.create_task(fake_run()))
    timed_out = await graceful_shutdown(gw, grace_seconds=2.0)
    assert gw.aborted
    assert timed_out is False


@pytest.mark.asyncio
async def test_drain_timeout_returns_true():
    gw = _StubGateway(finish_after=10.0)
    async def fake_run():
        await asyncio.sleep(10.0)
    gw.background_tasks.append(asyncio.create_task(fake_run()))
    timed_out = await graceful_shutdown(gw, grace_seconds=0.3)
    assert gw.aborted
    assert timed_out is True
```

- [ ] **Step 10.1: 写测试 + 跑确认失败**

Run: `pytest tests/test_sigterm_graceful_drain.py -v`
Expected: FAIL — `xana.cli.shutdown` 不存在。

- [ ] **Step 10.2: 加 `cli/shutdown.py`**

`src/xana/cli/shutdown.py`:

```python
"""Graceful shutdown helper(P5.6)。

`graceful_shutdown(gateway, grace_seconds)`:
  1. 调 `gateway.cancel_all()` 翻所有 background runs 的 abort
  2. 等 ≤ grace_seconds drain
  3. 超时返回 True(下次启动按 shutdown_force 进 /resume)
     正常 drain 返回 False
"""
from __future__ import annotations

import asyncio


async def graceful_shutdown(gateway, *, grace_seconds: float = 2.0) -> bool:
    await gateway.cancel_all()
    deadline = asyncio.get_event_loop().time() + grace_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not gateway.has_background():
            return False
        await asyncio.sleep(0.05)
    return True
```

- [ ] **Step 10.3: 测试通过**

Run: `pytest tests/test_sigterm_graceful_drain.py -v`
Expected: 2 PASS。

- [ ] **Step 10.4: gateway 加 `cancel_all` + `has_background`**

`agent_adapter/gateway.py` 加:

```python
async def cancel_all(self) -> None:
    """SIGTERM 入口 — 翻所有 inflight runs 的 abort,标 exit_reason='shutdown_graceful'。"""
    for runs in self._inflight.values():
        for ir in runs:
            await self.cancel(ir.session_id, ir.run_id)
            # 把 exit_reason 标 shutdown_graceful — 若后续 drain 超时,下次启动
            # 扫盘会按 run_completed marker 缺失改判 shutdown_force,自然覆盖
            rec = await self.run_store.get(ir.run_id)
            if rec is not None:
                from xana.state.contracts import RunRecord
                updated = RunRecord(
                    run_id=rec.run_id, session_id=rec.session_id,
                    agent_name=rec.agent_name, status=rec.status,
                    input_request=rec.input_request,
                    last_checkpoint=rec.last_checkpoint,
                    usage=rec.usage, pause_reason=rec.pause_reason,
                    abort_reason=rec.abort_reason, error=rec.error,
                    exit_reason="shutdown_graceful", metadata=rec.metadata,
                )
                await self.run_store.update(updated)

def has_background(self) -> bool:
    for runs in self._inflight.values():
        for ir in runs:
            if ir.task is not None and not ir.task.done():
                return True
    return False
```

- [ ] **Step 10.5: serve.py / xanac.py 注册 SIGTERM**

`src/xana/cli/serve.py`(serve 启动 main 函数末尾、`loop.run_forever` 之前):

```python
import signal
from xana.cli.shutdown import graceful_shutdown

def _sigterm_handler():
    asyncio.create_task(_handle_sigterm())

async def _handle_sigterm():
    timed_out = await graceful_shutdown(gateway, grace_seconds=2.0)
    if timed_out:
        import logging
        logging.warning("graceful shutdown 2s 超时 — 强制退出,未完 run 下次启动按 shutdown_force 处理")
    loop.stop()

loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
```

`xanac.py` 在 REPL 启动前同样 `loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)`(交互模式下 ctrl-C 已经走 KeyboardInterrupt,这里只处理 SIGTERM)。

- [ ] **Step 10.6: Commit**

```bash
git add src/xana/cli/shutdown.py src/xana/cli/serve.py src/xana/cli/xanac.py \
        src/xana/agent_adapter/gateway.py
git commit -m "feat(cli): SIGTERM graceful drain ≤2s with shutdown_force fallback (P5.6)"
cd tests && git add test_sigterm_graceful_drain.py && \
  git commit -m "test(cli): graceful_shutdown returns timed_out flag" && cd ..
```

---

## Task 11 — compact 卡去除 stop 按钮

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu_card.py`(compact 卡构造函数)
- Modify: `src/xana/agent_adapter/channels/telegram_html.py`(compact 消息文本)
- Test: `tests/test_compact_card_no_stop_button.py`

### Step 11.1 — 写测试

```python
# tests/test_compact_card_no_stop_button.py
"""compact 卡不再渲染 stop 按钮(P5.8 / 不可取消)。"""
from __future__ import annotations

from xana.agent_adapter.channels._draft import TurnDraft
from xana.agent_adapter.channels.feishu_card import build_compact_card


def test_feishu_compact_card_has_no_stop_button():
    draft = TurnDraft(run_id="compact-r1")
    draft.body = "已压缩 N 条"
    card = build_compact_card(draft, status="running")
    elements = card.get("body", {}).get("elements", []) \
               if "body" in card else card.get("elements", [])
    def _walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                yield node
            for v in node.values():
                yield from _walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from _walk(v)
    btns = list(_walk(card))
    labels = []
    for b in btns:
        txt = b.get("text", {})
        if isinstance(txt, dict):
            labels.append(txt.get("content", ""))
    assert all("停止" not in l and "stop" not in l.lower() and "取消" not in l for l in labels)
```

- [ ] **Step 11.1: 写测试 + 跑确认**

Run: `pytest tests/test_compact_card_no_stop_button.py -v`
Expected: FAIL 或 PASS — 若 P4 期已不渲染则 PASS,本任务作为回归 lock-in;若 FAIL 进 Step 11.2。

- [ ] **Step 11.2: 修 feishu / telegram 卡 stop 分支**

`feishu_card.py` 找 `build_compact_card`(或 compact 渲染分支),删除"running 时加 stop 按钮"那段,改为不加任何按钮。telegram 同步。

具体定位:grep `build_compact_card`,看现状哪里 append `{"tag":"button","text":{"content":"停止"}}`,删之。telegram_html.py 若现状直接拼了 "停止" 文本/链接也删掉。

- [ ] **Step 11.3: 测试通过 + commit**

```bash
git add src/xana/agent_adapter/channels/feishu_card.py \
        src/xana/agent_adapter/channels/telegram_html.py
git commit -m "feat(adapter): remove compact card stop button (P5.8 — compact uncancellable)"
cd tests && git add test_compact_card_no_stop_button.py && \
  git commit -m "test(adapter): compact card renders no stop button" && cd ..
```

---

## Task 12 — Sync + 人工 e2e

- [ ] **Step 12.1: 跑全套 P5 测试**

Run:

```bash
pytest tests/test_run_exit_reason_enum.py \
       tests/test_list_unfinished_filter.py \
       tests/test_tool_spec_read_only_doc.py \
       tests/test_dispatch_tool_cancel_readonly.py \
       tests/test_dispatch_tool_cancel_write_2s_grace.py \
       tests/test_dispatch_tool_force_kill_warning.py \
       tests/test_run_jsonl_rename_to_aborted.py \
       tests/test_resume_command_list.py \
       tests/test_resume_command_fork.py \
       tests/test_startup_scan_a_wins_over_b.py \
       tests/test_startup_scan_completed_replaces_ph.py \
       tests/test_startup_scan_b_enters_resume_list.py \
       tests/test_sigterm_graceful_drain.py \
       tests/test_compact_card_no_stop_button.py -v
```

Expected: ALL PASS。

- [ ] **Step 12.2: 跑回归(P1-P4 测试集)**

Run: `pytest tests/ -v -x`
Expected: ALL PASS。**任何回归都先停,定位影响面再继续。**

- [ ] **Step 12.3: `xana sync`(auto-restart serve)**

Run: `~/.xana/bin/xana sync`
Expected: `serve restarted at pid=...`。

- [ ] **Step 12.4: 手动 e2e(写进 PR 描述)**

1. session 内启一个 hang 写工具(可用 `bash sleep 10`)→ 按 stop / 发新消息 → 期待 2s 后 tool_result 出现中文 warning,模型不自动重试。
2. session 内启一个 hang 只读工具(可用 `bash sleep 10` 但伪 `read_only=True`)→ stop → 立即返回 cancelled。
3. `kill -TERM <serve_pid>` → 后台有正在跑的 run → serve 在 2s 内退出;下次 `xana serve` 启动后 `/resume` 列表显示该 run(exit_reason=shutdown_graceful 或 shutdown_force)。
4. `/resume <run_id>` → 同 user input 重新跑;原 `runs/{run_id}.jsonl` 改名 `.aborted.jsonl`。
5. `/compact` 卡上**无 stop 按钮**。

- [ ] **Step 12.5: 写 PR**

PR 标题:`feat(phase5): Cancel/Resume/重启恢复 + graceful SIGTERM`
PR body 引用 `docs/superpowers/specs/2026-06-14-p5-p6-decisions.md` §P5 与本 plan 路径,贴 Step 12.4 的 5 步 e2e 截图/日志。

---

## 不在本 plan 范围

- **P6 async-aware compact**(独立 plan 处理)。
- **PH 改两节点存储**(属 P6.1,会改 submit ritual;P5 不动 PH 存储)。
- **post-compact 引用 main.jsonl 的 CC-style 文本提示**(P6.4)。
- **子分支 compact**(P6.3/6.5)。
- **CLI Textual REPL 上 PH-first attach UX + tail-3 卡片布局**(独立 UI 整包)。

---

## Self-Review

**Spec coverage(`2026-06-14-p5-p6-decisions.md` §P5):**
- P5.1 ToolSpec.read_only 注释 → Task 3 ✓
- P5.2 synthetic warning 中文文案 → Task 4(常量定义)+ Task 6(测试覆盖) ✓
- P5.3 .aborted.jsonl 改名 → Task 7 + Task 8 调用 ✓
- P5.4 ExitReason 枚举 + 过滤 → Task 1 + Task 2 ✓
- P5.5 startup 扫盘 A 赢 → Task 9 ✓
- P5.6 SIGTERM 2s drain + shutdown_force → Task 10 + Task 9.8 ✓
- P5.7 不引入 triggers_turn 通用字段 → 本 plan 不动通用字段,通过 Task 11 单独删 compact 按钮 ✓
- P5.8 compact 不可取消 → Task 11 ✓
- P5.9 文案固化 → Task 4 Step 4.3 常量 ✓

**Placeholder scan:** 无 TBD/TODO/"similar to Task N";`_submit_internal` / `_runs_root` / `self._abort_signal` 等字段名以"按实际改"标注 — 实施时对照 survey 报告的真名替换,**不是占位**。

**Type consistency:**
- `ExitReason` 名称在 Task 1/2/9/10 一致。
- `_run_tool_with_abort` / `_SYNTHETIC_WARNING_TEXT_KILLED` / `_SYNTHETIC_WARNING_TEXT_READONLY` 在 Task 4-6 一致。
- `scan_runs_dir` / `replace_completed_placeholders` / `mark_unfinished_as_resumable` / `ScanReport` 在 Task 9 各 step 一致。
- `graceful_shutdown(gateway, grace_seconds)` 在 Task 10 各 step 一致。
- `gateway.cancel_all` / `gateway.has_background` / `gateway.resume` / `gateway.run_store` / `gateway._inflight` 命名一致。

**接口改动审批门**(`feedback_interfaces_locked.md`):
- 本 plan 触碰的 contracts/interfaces 改动:
  - `state/contracts.py` 新加 `ExitReason` Literal + `RunRecord.exit_reason` 字段(Task 1)。
  - `state/contracts.py` 末尾导出 `RESUMABLE_EXIT_REASONS` / `EXCLUDED_EXIT_REASONS` 常量(Task 2)。
  - `state/interfaces.py` `RunStore.list_unfinished` docstring 补语义(Task 2,非签名改动)。
  - `tools/contracts.py` `ToolSpec.read_only` 仅加注释,字段不动(Task 3)。
- **实施期开 Task 1/2/3 前先与用户确认这几条字段/常量/注释改动通过**(此条已在 plan 显式列出,实施 agent 必须落地审批)。
