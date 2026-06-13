# Tools & Skills Checklist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `/home/xzp/project/best_agent/ds_review/xana_review/tools-skills-checklist.md` 补齐 xana 的工具与技能:deferred tools 机制 + `tool_search`、`web_search`、`web_fetch`、`cron_create/list/delete`(FileSchedulerService)、`skill_load` + 3 个 skill(loop / skill_creator / schedule 本地适配);做不了的输出到 `todo_ai/deferred_features.md`。

**Architecture:** 全部复用已有设计:工具实现 `Tool` ABC(惯用法照抄 `tools/builtin/enter_plan_mode.py`),在 `boot()` 注册进 `InMemoryToolRegistry`;cron 三工具构造注入 `SchedulerService`(ABC 已在 `extension_api/capabilities.py`,本计划提供文件持久化实现);web_search 的 provider 读已有 `SessionConfig.search_provider`(构造注入 session_config_store,同 EnterPlanModeTool);skills 用已有 `Skill` ABC + `InMemorySkillRegistry`,新增 `skill_load` 工具按需取全文,可用技能列表由 `TurnInputContributor` 扩展注入。锁定文件唯一授权改动:`ToolSpec` 加 `defer: bool = False`(用户 2026-06-13 批准)。

**Tech Stack:** Python 3.12, pytest-asyncio, httpx(已有), 新依赖:`croniter`(cron 解析)、`ddgs`/`duckduckgo_search`(免密搜索)、`beautifulsoup4`(网页正文抽取,缺失时回退正则)。

---

## 用户已拍板的决策(2026-06-13)

1. **ToolSpec.defer 授权**:`src/xana/tools/contracts.py` 只加 `defer: bool = False` 一个字段,其余一字不动。revealed 状态走消息 metadata,不再改任何锁定文件。
2. **skill 加载 = skill_load 工具**(CC 同款):每轮注入「# 可用技能」名字+描述列表,模型按需调 `skill_load(name)` 拿 SKILL.md 全文。
3. **cron 文件持久化**:`~/.xana/scheduled_tasks.json`,serve 重启任务仍在;到点经 gateway 把 prompt 注入对应 session。
4. **schedule skill 本地适配**:指导模型用 cron_create/list/delete 管理本地定时任务,保留 CC 的交互流程;remote 部分进 todo md。

## 规格来源(只读)

- 主规格:`/home/xzp/project/best_agent/ds_review/xana_review/tools-skills-checklist.md`
- yuxu 参考(web_search/web_fetch 逻辑直接抄,适配 Tool ABC):`/home/xzp/project/yuxu_backup/src/yuxu/services/web_search/handler.py`、`/home/xzp/project/yuxu_backup/src/yuxu/services/web_fetch/handler.py`
- CC 源(schema/描述文本逐语义翻译为中文):`/home/xzp/project/best_agent/claude-code-2.1.88-src/tools/ToolSearchTool/`、`tools/WebFetchTool/`、`tools/ScheduleCronTool/`、`skills/bundled/{loop,skillify,scheduleRemoteAgents}.ts`

## 硬约束

- 除 Task 1 的 `defer` 字段外,**不得编辑任何 `*/contracts.py` / `*/interfaces.py`**(含 `extension_api/capabilities.py` —— SchedulerService ABC 原样用,签名不够用就降级适配,不改 ABC)。
- 注册只在 `boot()`(serve 的 SchedulerRunner 启动在 `cli/serve.py`,不算 register)。
- 工具 schema/描述 = CC 原样语义的中文翻译;web_search/web_fetch 执行逻辑抄 yuxu。
- 每个文件独立可 import;构造注入,无全局单例。
- 全程本地 commit,**不 push**;每个任务结束跑全量 pytest(已知可忽略失败:`tests/test_xanac_e2e.py::test_xanac_empty_input_exits_nonzero`)。

## File Structure

```
Modify: src/xana/tools/contracts.py            (仅 +defer 字段,已授权)
Modify: src/xana/harness_sdk/default.py        (deferred 分离 + revealed 扫描)
Modify: src/xana/harness_sdk/system_prompt.py  (deferred_tools_section 实装)
Create: src/xana/tools/builtin/tool_search.py
Create: src/xana/runtime/scheduler.py          (FileSchedulerService + SchedulerRunner)
Create: src/xana/tools/builtin/cron.py         (CronCreate/CronList/CronDelete 三个类)
Create: src/xana/tools/builtin/web_search.py
Create: src/xana/tools/builtin/web_fetch.py
Create: src/xana/tools/builtin/skill_load.py
Create: src/xana/skills/bundled/{__init__.py,loop.py,skill_creator.py,schedule.py}
Create: src/xana/extension_api/builtin/skills_listing.py (TurnInputContributor)
Modify: src/xana/runtime/boot.py               (注册新工具/技能/扩展)
Modify: src/xana/cli/serve.py                  (启动 SchedulerRunner)
Create: todo_ai/deferred_features.md           (做不了的汇总)
Tests:  tests/test_deferred_tools.py, tests/test_tool_search.py, tests/test_scheduler.py,
        tests/test_cron_tools.py, tests/test_web_search_tool.py, tests/test_web_fetch_tool.py,
        tests/test_skill_load.py, tests/test_bundled_skills.py
```

---

### Task 1: ToolSpec.defer + harness deferred 分离 + prompt 按需工具段

**Files:**
- Modify: `src/xana/tools/contracts.py:50-63`(只加一个字段)
- Modify: `src/xana/harness_sdk/default.py`(tools_schema 组装,约 384-397 行;system prompt 调用,约 368-373 行)
- Modify: `src/xana/harness_sdk/system_prompt.py`(`deferred_tools_section` 实装)
- Test: `tests/test_deferred_tools.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_deferred_tools.py
"""deferred tools: defer=True 的工具不发 schema,名字进「# 按需工具」段;
tool_search 揭示后(消息 metadata revealed_tools)恢复完整 schema。"""
from __future__ import annotations

import pytest

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.harness_sdk.system_prompt import deferred_tools_section
from xana.tools.contracts import ToolSpec


def _spec(name: str, defer: bool) -> ToolSpec:
    return ToolSpec(
        component=ComponentSpec(
            kind="tool", name=name, qualified_name=f"t.{name}",
            description=f"{name} 描述", source=ComponentSource(source_type="builtin", scope="system")),
        input_schema={"type": "object", "properties": {}},
        defer=defer,
    )


def test_tool_spec_defer_defaults_false() -> None:
    assert _spec("a", defer=False).defer is False
    assert _spec("b", defer=True).defer is True


def test_deferred_tools_section_renders_template() -> None:
    out = deferred_tools_section((("web_search", "联网搜索"), ("cron_list", "列出定时任务")))
    assert out.startswith("# 按需工具\n")
    assert "tool_search" in out
    assert "- web_search: 联网搜索" in out
    assert "- cron_list: 列出定时任务" in out


def test_deferred_tools_section_empty_returns_empty() -> None:
    assert deferred_tools_section(()) == ""


def test_split_tool_schemas_separates_deferred_until_revealed() -> None:
    from xana.harness_sdk.default import _split_tool_schemas
    specs = [_spec("read", False), _spec("web_search", True), _spec("cron_create", True)]
    schemas, deferred = _split_tool_schemas(specs, revealed=set())
    assert [s["name"] for s in schemas] == ["read"]
    assert [n for n, _ in deferred] == ["web_search", "cron_create"]
    schemas2, deferred2 = _split_tool_schemas(specs, revealed={"web_search"})
    assert [s["name"] for s in schemas2] == ["read", "web_search"]
    assert [n for n, _ in deferred2] == ["cron_create"]


def test_revealed_tools_recovered_from_history() -> None:
    from xana.harness_sdk.default import _revealed_tools
    from xana.memory.contracts import MemoryMessage
    msgs = (
        MemoryMessage(role="user", content="hi"),
        MemoryMessage(role="tool", content="…",
                      metadata={"tool_name": "tool_search", "revealed_tools": ["web_search"]}),
        MemoryMessage(role="tool", content="…",
                      metadata={"tool_name": "tool_search", "revealed_tools": ["cron_create"]}),
    )
    assert _revealed_tools(msgs) == {"web_search", "cron_create"}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/xzp/project/xana && python -m pytest tests/test_deferred_tools.py -x -q`
Expected: FAIL(ToolSpec 无 defer / 函数不存在)

- [ ] **Step 3: 实现**

`tools/contracts.py` ToolSpec 字段区末尾(`user_facing_name` 之后)加:

```python
    defer: bool = False  # True = 隐藏工具:模型只见名字,经 tool_search 揭示后才发完整 schema
```

`harness_sdk/system_prompt.py` 替换 `deferred_tools_section`(模板逐字 AGENT_LOOP_PROMPT.md:166-170,保留 TODO 注释删除):

```python
def deferred_tools_section(deferred: tuple[tuple[str, str], ...] = ()) -> str:
    """# 按需工具 段(模板 AGENT_LOOP_PROMPT.md:166-170)。deferred = (名字, 描述) 列表。"""
    if not deferred:
        return ""
    lines = "\n".join(f"- {name}: {desc}" for name, desc in deferred)
    return ("# 按需工具\n\n"
            "以下工具默认隐藏。使用 tool_search 查看完整 schema 并按需加载：\n\n" + lines)
```

`harness_sdk/default.py` 模块级加两个纯函数:

```python
def _split_tool_schemas(
    specs, revealed: set[str],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """defer 且未揭示 → 只给 (名字, 描述);其余给完整 schema。"""
    schemas: list[dict] = []
    deferred: list[tuple[str, str]] = []
    for s in specs:
        if getattr(s, "defer", False) and s.component.name not in revealed:
            deferred.append((s.component.name, s.component.description or ""))
        else:
            schemas.append(_to_anthropic_tool_schema(s))
    return schemas, deferred


def _revealed_tools(messages) -> set[str]:
    """从消息历史恢复已揭示工具(tool_search 结果的 metadata.revealed_tools)。
    CC 同款思路:揭示状态存在历史里,不是单独状态变量。"""
    out: set[str] = set()
    for m in messages:
        names = m.metadata.get("revealed_tools")
        if names:
            out.update(names)
    return out
```

`_invoke` 中接线(两处):
1. tools_schema 组装处(现约 384-397 行):先 `snapshot0 = await context.active_context_store.get(session_id, agent_name)` 取当前历史算 `revealed = _revealed_tools(snapshot0.messages if snapshot0 else ())`;`specs = await context.tool_registry.list_specs()` 后改为 `schemas, deferred_names = _split_tool_schemas(specs, revealed)`、`tools_schema = tuple(schemas)`;extension 工具合并逻辑保持(extension 工具不参与 defer)。
2. system prompt 调用处:`build_system_prompt(..., env=..., dynamic_middle=...)` 之前把 `deferred_tools_section(tuple(deferred_names))` 作为 dynamic_middle 的第一个元素传入(顺序:deferred 段在 extension 中段之前,符合 13 段第 7 位)。注意 tools_schema 组装需移到 system_content 之前(目前在其后)——把组装块上移即可,无其他行为变化。

同时每个 turn 循环内 revealed 需重算吗?**需要**:tool_search 在本 turn 揭示后,下一轮 model_request 就要带新 schema。把 revealed/`_split_tool_schemas`/`build_system_prompt` 的调用移进 `for turn_index` 循环开头(history_msgs 取出之后),用 history_msgs 算 revealed —— system_content 也随之每轮重拼(环境段函数已无状态,开销可忽略)。

`tests/test_system_prompt.py` 的 `test_tools_and_deferred_sections_are_empty_for_now` 改名为 `test_tools_section_empty_and_deferred_renders`,断言 `sp.tools_section() == ""`、`sp.deferred_tools_section(()) == ""`。

- [ ] **Step 4: 运行测试 + 全量回归**

Run: `python -m pytest tests/test_deferred_tools.py tests/test_system_prompt.py tests/test_harness_system_prompt.py -q && python -m pytest -q`
Expected: 通过(已知 e2e 除外)

- [ ] **Step 5: Commit**

```bash
git add -A src/xana/tools/contracts.py src/xana/harness_sdk/ tests/
git commit -m "feat(tools): ToolSpec.defer(已授权)+ harness deferred 分离 + 按需工具段"
```

---

### Task 2: tool_search 工具

**Files:**
- Create: `src/xana/tools/builtin/tool_search.py`
- Test: `tests/test_tool_search.py`
- Modify: `src/xana/runtime/boot.py`(注册)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_search.py
import json

import pytest

from xana.tools.builtin.tool_search import ToolSearchTool
from xana.tools.contracts import ToolContext, ToolInvocation
from xana.tools.registry import InMemoryToolRegistry
from xana.policy.contracts import PermissionContext

# 复用 tests/test_deferred_tools.py 的 _spec;这里用最小 stub Tool 注册 deferred 工具


class _StubTool:
    def __init__(self, spec):
        self._spec = spec

    def spec(self):
        return self._spec


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", agent_name="chat", cwd="/tmp",
                       permission=PermissionContext(mode="default", session_id="s"))


def _inv(query: str, **kw) -> ToolInvocation:
    return ToolInvocation(tool_name="tool_search", input={"query": query, **kw},
                          call_id="c1", session_id="s")


async def _registry():
    from tests.test_deferred_tools import _spec
    reg = InMemoryToolRegistry()
    await reg.register(_StubTool(_spec("web_search", defer=True)))
    await reg.register(_StubTool(_spec("cron_create", defer=True)))
    await reg.register(_StubTool(_spec("read", defer=False)))
    return reg


@pytest.mark.asyncio
async def test_select_mode_reveals_exact_tools() -> None:
    tool = ToolSearchTool(tool_registry=await _registry())
    res = await tool.execute(_inv("select:web_search,cron_create"), _ctx())
    assert res.ok
    assert set(res.metadata["revealed_tools"]) == {"web_search", "cron_create"}
    assert "<functions>" in (res.rendered or "")
    assert '"name": "web_search"' in res.rendered


@pytest.mark.asyncio
async def test_keyword_search_scores_and_limits() -> None:
    tool = ToolSearchTool(tool_registry=await _registry())
    res = await tool.execute(_inv("cron", max_results=1), _ctx())
    assert res.ok and res.metadata["revealed_tools"] == ["cron_create"]


@pytest.mark.asyncio
async def test_no_match_reports_zero() -> None:
    tool = ToolSearchTool(tool_registry=await _registry())
    res = await tool.execute(_inv("不存在的能力"), _ctx())
    assert res.ok and res.metadata["revealed_tools"] == []
    assert "未匹配" in res.rendered


@pytest.mark.asyncio
async def test_non_deferred_tools_not_searchable() -> None:
    tool = ToolSearchTool(tool_registry=await _registry())
    res = await tool.execute(_inv("select:read"), _ctx())
    assert res.metadata["revealed_tools"] == []
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_tool_search.py -x -q`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 3: 实现**

`src/xana/tools/builtin/tool_search.py` — 类结构照抄 `enter_plan_mode.py` 惯用法(validate 校验 query 非空;check_permission 恒 allow;execute 如下;render_result 返回 rendered)。描述文本 = CC ToolSearchTool prompt 的中文翻译:

```python
_DESCRIPTION = (
    "获取隐藏工具(deferred tools)的完整 schema 定义,使其可被调用。\n"
    "隐藏工具只在「# 按需工具」段以名字出现——在获取前没有参数 schema,无法调用。"
    "本工具接收一个查询,与隐藏工具列表匹配,在 <functions> 块内返回匹配工具的完整 "
    "JSONSchema 定义。一旦某工具的 schema 出现在结果中,它就和提示顶部定义的工具一样可直接调用。\n"
    "查询形式:\n"
    '- "select:web_search,cron_create" —— 按名字精确获取\n'
    '- "网页 搜索" —— 关键词搜索,返回至多 max_results 个最佳匹配\n'
    '- "+cron 创建" —— 名字必须含 "cron",其余词参与排序'
)

_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool", name="tool_search", qualified_name="builtin.tool_search",
        description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": '查询。"select:<名字>" 精确选择,或关键词搜索。'},
            "max_results": {"type": "number", "description": "最多返回数(默认 5)"},
        },
        "required": ["query"],
    },
    read_only=True,
)
```

execute 核心(评分规则 = CC 简化版:名字精确部分=10,名字子串=5,描述含词=2;`+词` 必须出现在名字里):

```python
    async def execute(self, invocation, context):
        query = str(invocation.input.get("query", "")).strip()
        max_results = int(invocation.input.get("max_results", 5) or 5)
        specs = [s for s in await self.tool_registry.list_specs() if getattr(s, "defer", False)]
        by_name = {s.component.name: s for s in specs}
        if query.startswith("select:"):
            names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            matched = [by_name[n] for n in names if n in by_name]
        else:
            terms = query.split()
            required = [t[1:].lower() for t in terms if t.startswith("+")]
            optional = [t.lower() for t in terms if not t.startswith("+")]
            scored = []
            for s in specs:
                name = s.component.name.lower()
                desc = (s.component.description or "").lower()
                if any(r not in name for r in required):
                    continue
                score = sum(10 if t == name else 5 if t in name else 2 if t in desc else 0
                            for t in (optional or required))
                if score > 0:
                    scored.append((score, s))
            scored.sort(key=lambda x: -x[0])
            matched = [s for _, s in scored[:max_results]]
        revealed = [s.component.name for s in matched]
        if not matched:
            rendered = f"未匹配到隐藏工具(共 {len(specs)} 个可搜)。换关键词或用 select:<名字>。"
        else:
            lines = "\n".join(
                "<function>" + json.dumps(
                    {"description": s.component.description or "",
                     "name": s.component.name,
                     "parameters": dict(s.input_schema)},
                    ensure_ascii=False) + "</function>"
                for s in matched)
            rendered = f"<functions>\n{lines}\n</functions>"
        return ToolResult(ok=True, content={"matches": revealed, "query": query,
                                            "total_deferred_tools": len(specs)},
                          rendered=rendered, metadata={"revealed_tools": revealed})
```

**关键接线**:harness 把 ToolResult 写成 MemoryMessage 时 metadata 目前只带 tool_use_id/tool_name/ok(default.py:562-570)。改为同时透传 `revealed_tools`:

```python
                tool_meta = {"tool_use_id": tc["call_id"], "tool_name": tc["tool_name"], "ok": result.ok}
                if result.metadata.get("revealed_tools"):
                    tool_meta["revealed_tools"] = list(result.metadata["revealed_tools"])
                tool_msg = MemoryMessage(role="tool", content=rendered, metadata=tool_meta)
```

boot.py 注册(`await tools.register(BashTool())` 之后):

```python
    await tools.register(ToolSearchTool(tool_registry=tools))
```

- [ ] **Step 4: 测试 + 回归**

Run: `python -m pytest tests/test_tool_search.py tests/test_deferred_tools.py -q && python -m pytest -q`
Expected: 通过

- [ ] **Step 5: 端到端测试(harness 级)**

在 `tests/test_deferred_tools.py` 追加:FakeModelClient 两回合脚本(第一回合 tool_call tool_search select:web_search,第二回合普通文本),断言第二次 model_request 的 tools_schema 含 web_search 完整 schema 且「# 按需工具」段不再列它。参照 tests/test_default_harness.py 的工具回环测试构造。

Run: `python -m pytest tests/test_deferred_tools.py -q`
Expected: 通过

- [ ] **Step 6: Commit**

```bash
git add src/xana/tools/builtin/tool_search.py src/xana/runtime/boot.py src/xana/harness_sdk/default.py tests/
git commit -m "feat(tools): tool_search 揭示隐藏工具(CC 同款 select/关键词/评分)"
```

---

### Task 3: FileSchedulerService(实现已有 SchedulerService ABC)

**Files:**
- Create: `src/xana/runtime/scheduler.py`
- Test: `tests/test_scheduler.py`
- Modify: `pyproject.toml`(croniter 依赖)

- [ ] **Step 1: 依赖**

Run: `grep -n "croniter" pyproject.toml || true`;若无,在 dependencies 数组加 `"croniter>=2.0"`,然后 `pip install croniter`。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_scheduler.py
import json
from pathlib import Path

import pytest

from xana.runtime.scheduler import FileSchedulerService


@pytest.mark.asyncio
async def test_add_list_remove_roundtrip(tmp_path: Path) -> None:
    svc = FileSchedulerService(path=tmp_path / "scheduled_tasks.json", session_id="s1")
    task_id = await svc.add(cron="*/5 * * * *", prompt="检查 CI", recurring=True)
    jobs = await svc.list_all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == task_id and job["cron"] == "*/5 * * * *"
    assert job["prompt"] == "检查 CI" and job["recurring"] is True
    await svc.remove(task_id)
    assert await svc.list_all() == ()


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "scheduled_tasks.json"
    await FileSchedulerService(path=p, session_id="s1").add(cron="0 9 * * 1-5", prompt="晨报")
    jobs = await FileSchedulerService(path=p, session_id="s1").list_all()
    assert len(jobs) == 1 and jobs[0]["prompt"] == "晨报"


@pytest.mark.asyncio
async def test_invalid_cron_rejected(tmp_path: Path) -> None:
    svc = FileSchedulerService(path=tmp_path / "t.json", session_id="s1")
    with pytest.raises(ValueError):
        await svc.add(cron="not a cron", prompt="x")


@pytest.mark.asyncio
async def test_due_jobs_oneshot_autodeletes(tmp_path: Path) -> None:
    svc = FileSchedulerService(path=tmp_path / "t.json", session_id="s1")
    await svc.add(cron="* * * * *", prompt="一次性", recurring=False)
    due = await svc.pop_due(now_epoch=4102444800.0)  # 2100-01-01,必然过了下次触发点
    assert [j["prompt"] for j in due] == ["一次性"]
    assert await svc.list_all() == ()  # 一次性触发后自动删除


@pytest.mark.asyncio
async def test_due_jobs_recurring_reschedules(tmp_path: Path) -> None:
    svc = FileSchedulerService(path=tmp_path / "t.json", session_id="s1")
    await svc.add(cron="* * * * *", prompt="循环", recurring=True)
    due = await svc.pop_due(now_epoch=4102444800.0)
    assert len(due) == 1
    jobs = await svc.list_all()
    assert len(jobs) == 1 and jobs[0]["next_fire"] > 4102444800.0
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/test_scheduler.py -x -q`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 4: 实现 `src/xana/runtime/scheduler.py`**

```python
"""FileSchedulerService — extension_api.capabilities.SchedulerService 的文件持久化实现。

存储 ~/.xana/scheduled_tasks.json(boot 注入实际路径)。每条任务:
{id, cron, human, prompt, recurring, session_id, created_at, next_fire}
next_fire 用 croniter 由 cron 表达式算出(本地时区)。SchedulerRunner(serve 内
asyncio 任务)定期调 pop_due() 并把 prompt 经 gateway 注入对应 session。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from croniter import croniter

from xana.extension_api.capabilities import SchedulerService


class FileSchedulerService(SchedulerService):
    def __init__(self, *, path: Path, session_id: str) -> None:
        self._path = path
        self._session_id = session_id  # cron 工具按 session 注入;runner 读任务自带的 session_id

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, jobs: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")

    async def add(self, *, cron: str, prompt: str, recurring: bool = False) -> str:
        if not croniter.is_valid(cron):
            raise ValueError(f"非法 cron 表达式: {cron!r}")
        now = time.time()
        task_id = uuid.uuid4().hex[:8]
        jobs = self._load()
        jobs.append({
            "id": task_id, "cron": cron, "prompt": prompt, "recurring": recurring,
            "session_id": self._session_id, "created_at": now,
            "next_fire": croniter(cron, now).get_next(float),
        })
        self._save(jobs)
        return task_id

    async def remove(self, task_id: str) -> None:
        self._save([j for j in self._load() if j["id"] != task_id])

    async def list_all(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._load())

    async def pop_due(self, *, now_epoch: float | None = None) -> tuple[dict[str, Any], ...]:
        """到点任务:一次性的删除,循环的滚动 next_fire。SchedulerRunner 专用。"""
        now = now_epoch if now_epoch is not None else time.time()
        jobs = self._load()
        due = [j for j in jobs if j["next_fire"] <= now]
        keep = [j for j in jobs if j["next_fire"] > now]
        for j in due:
            if j["recurring"]:
                j["next_fire"] = croniter(j["cron"], now).get_next(float)
                keep.append(j)
        self._save(keep)
        return tuple(due)
```

注:ABC 没有 pop_due —— 这是实现类自有方法,runner 持有具体类型(构造注入),不经 ABC 调用,合规。

- [ ] **Step 5: 测试 + Commit**

Run: `python -m pytest tests/test_scheduler.py -q && python -m pytest -q`

```bash
git add src/xana/runtime/scheduler.py tests/test_scheduler.py pyproject.toml
git commit -m "feat(runtime): FileSchedulerService 文件持久化实现 SchedulerService ABC"
```

---

### Task 4: cron_create / cron_list / cron_delete 三工具(deferred)

**Files:**
- Create: `src/xana/tools/builtin/cron.py`(三个类一个文件——同属一个能力,变更同源)
- Test: `tests/test_cron_tools.py`
- Modify: `src/xana/runtime/boot.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cron_tools.py
from pathlib import Path

import pytest

from xana.policy.contracts import PermissionContext
from xana.runtime.scheduler import FileSchedulerService
from xana.tools.builtin.cron import CronCreateTool, CronDeleteTool, CronListTool
from xana.tools.contracts import ToolContext, ToolInvocation


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", agent_name="chat", cwd="/tmp",
                       permission=PermissionContext(mode="default", session_id="s"))


def _inv(name: str, **inp) -> ToolInvocation:
    return ToolInvocation(tool_name=name, input=inp, call_id="c", session_id="s")


def _svc(tmp_path: Path) -> FileSchedulerService:
    return FileSchedulerService(path=tmp_path / "t.json", session_id="s")


@pytest.mark.asyncio
async def test_create_list_delete_flow(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    create, lst, delete = CronCreateTool(svc), CronListTool(svc), CronDeleteTool(svc)
    r = await create.execute(_inv("cron_create", cron="*/5 * * * *", prompt="查 CI", recurring=True), _ctx())
    assert r.ok and r.content["id"]
    r2 = await lst.execute(_inv("cron_list"), _ctx())
    assert r2.ok and len(r2.content["jobs"]) == 1
    assert r2.content["jobs"][0]["cron"] == "*/5 * * * *"
    r3 = await delete.execute(_inv("cron_delete", id=r.content["id"]), _ctx())
    assert r3.ok
    r4 = await lst.execute(_inv("cron_list"), _ctx())
    assert r4.content["jobs"] == []


@pytest.mark.asyncio
async def test_create_rejects_bad_cron(tmp_path: Path) -> None:
    r = await CronCreateTool(_svc(tmp_path)).execute(
        _inv("cron_create", cron="bad", prompt="x"), _ctx())
    assert not r.ok and r.failure_kind == "validation"


def test_all_three_are_deferred(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for t in (CronCreateTool(svc), CronListTool(svc), CronDeleteTool(svc)):
        assert t.spec().defer is True
```

- [ ] **Step 2: 确认失败**

Run: `python -m pytest tests/test_cron_tools.py -x -q` → FAIL

- [ ] **Step 3: 实现 `src/xana/tools/builtin/cron.py`**

三个类,结构照抄 enter_plan_mode.py 惯用法,构造注入 `SchedulerService`(类型标注用 ABC)。spec 全部 `defer=True`。描述/schema = CC ScheduleCronTool 中文翻译:

```python
_CREATE_SPEC = ToolSpec(
    component=ComponentSpec(
        kind="tool", name="cron_create", qualified_name="builtin.cron_create",
        description=(
            "调度一个 prompt 在未来运行——按 cron 表达式循环触发,或在指定时间触发一次。"
            "任务持久化到 ~/.xana/scheduled_tasks.json,serve 重启后仍然有效。\n"
            "cron 语法为本地时区的标准 5 字段:\"分 时 日 月 周\"。"
            "一次性任务(recurring=false)把分/时/日/月固定为具体值,触发一次后自动删除。"
            "循环任务示例:\"*/5 * * * *\" 每 5 分钟,\"0 * * * *\" 每小时,\"0 9 * * 1-5\" 工作日 9 点。"
            "避免 :00 和 :30 整点——选错峰分钟(如 :57、:03、:07)。"),
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "cron": {"type": "string",
                     "description": '本地时区标准 5 字段 cron 表达式:"分 时 日 月 周"'
                                    '(如 "*/5 * * * *" = 每 5 分钟)。'},
            "prompt": {"type": "string", "description": "每次触发时注入的 prompt。"},
            "recurring": {"type": "boolean",
                          "description": "true(默认)= 每次匹配都触发直至删除;"
                                         "false = 下次匹配触发一次后自动删除。"},
        },
        "required": ["cron", "prompt"],
    },
    defer=True,
)
```

`CronCreateTool.execute`:try `task_id = await self.scheduler.add(cron=…, prompt=…, recurring=bool(input.get("recurring", True)))`,ValueError → `ToolResult(ok=False, failure_kind="validation", error=str(e), rendered=f"[cron_create 失败: {e}]")`;成功 → `content={"id": task_id, "cron": cron, "recurring": recurring}`,`rendered=f"[cron_create] 已调度 {task_id}: {cron} → {prompt[:50]}"`。

`cron_list`:description "列出 cron_create 调度的全部定时任务(含持久化的)。",input_schema 空 object,execute → `jobs = await self.scheduler.list_all()`,content={"jobs": [{id,cron,prompt,recurring} …]},rendered 为每行 `- {id} {cron} recurring={recurring}: {prompt[:60]}` 或 "(无定时任务)"。

`cron_delete`:description "按 ID 取消 cron_create 调度的定时任务。",input_schema {"id": {"type":"string","description":"cron_create 返回的任务 ID。"}} required ["id"],execute → `await self.scheduler.remove(id)`,content={"id": id},rendered "[cron_delete] 已删除 {id}"。

boot.py(tool_search 注册之后):

```python
    scheduler = FileSchedulerService(
        path=Path.home() / ".xana" / "scheduled_tasks.json", session_id="default")
    await tools.register(CronCreateTool(scheduler))
    await tools.register(CronListTool(scheduler))
    await tools.register(CronDeleteTool(scheduler))
```

注:工具执行时的真实 session 来自 `ToolContext.session_id` —— `CronCreateTool.execute` 里若 `context.session_id` 非空,调 `self.scheduler.add` 前用 `dataclasses.replace` 不可行(svc 非 dataclass),改为 FileSchedulerService.add 增加可选参数?**不行,会破坏 ABC 签名**。正确做法:FileSchedulerService 构造的 `session_id` 仅是缺省;任务记录的 session 在 `add` 时无法传 → 在 `CronCreateTool.__init__` 注入一个 `scheduler_factory: Callable[[str], SchedulerService]`?过度设计。**裁定:任务的 session_id 由工具写**——FileSchedulerService 增加公有属性 `self.session_id`(可变),CronCreateTool.execute 开头 `self.scheduler.session_id = context.session_id`(runner 串行消费,无并发风险;在 cron.py 注释说明)。BootResult 加 `scheduler` 字段供 serve 使用。

- [ ] **Step 4: 测试 + 回归 + Commit**

Run: `python -m pytest tests/test_cron_tools.py -q && python -m pytest -q`

```bash
git add src/xana/tools/builtin/cron.py src/xana/runtime/boot.py tests/test_cron_tools.py
git commit -m "feat(tools): cron_create/list/delete 三个 deferred 工具(注入 SchedulerService)"
```

---

### Task 5: SchedulerRunner — serve 内到点触发

**Files:**
- Modify: `src/xana/runtime/scheduler.py`(追加 SchedulerRunner)
- Modify: `src/xana/cli/serve.py`(启动/停止 runner)
- Modify: `src/xana/runtime/boot.py`(BootResult.scheduler)
- Test: `tests/test_scheduler.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_scheduler.py

@pytest.mark.asyncio
async def test_runner_dispatches_due_prompt_via_gateway(tmp_path: Path) -> None:
    from xana.runtime.scheduler import SchedulerRunner

    svc = FileSchedulerService(path=tmp_path / "t.json", session_id="sess-9")
    await svc.add(cron="* * * * *", prompt="到点了,做事", recurring=False)

    sent = []

    class _GatewayStub:
        async def handle_text(self, *, session_id: str, text: str) -> None:
            sent.append((session_id, text))

    runner = SchedulerRunner(scheduler=svc, dispatch=_GatewayStub().handle_text,
                             poll_interval_s=0.01)
    await runner.tick(now_epoch=4102444800.0)
    assert sent == [("sess-9", "到点了,做事")]
```

- [ ] **Step 2: 确认失败** → `python -m pytest tests/test_scheduler.py -x -q`

- [ ] **Step 3: 实现**

scheduler.py 追加:

```python
import asyncio
import logging
from typing import Awaitable, Callable

_logger = logging.getLogger(__name__)


class SchedulerRunner:
    """serve 内的轮询器:每 poll_interval_s 调一次 tick(),把到点任务的 prompt
    经 dispatch 回调(gateway 注入)送进任务记录的 session。dispatch 异常只记日志
    ——一个失败任务不能杀死 runner。"""

    def __init__(self, *, scheduler: FileSchedulerService,
                 dispatch: Callable[..., Awaitable[None]],
                 poll_interval_s: float = 30.0) -> None:
        self._scheduler = scheduler
        self._dispatch = dispatch
        self._interval = poll_interval_s
        self._task: asyncio.Task | None = None

    async def tick(self, *, now_epoch: float | None = None) -> None:
        for job in await self._scheduler.pop_due(now_epoch=now_epoch):
            try:
                await self._dispatch(session_id=job["session_id"], text=job["prompt"])
            except Exception:
                _logger.exception("scheduled job %s dispatch failed", job["id"])

    def start(self) -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._interval)
                await self.tick()
        self._task = asyncio.create_task(_loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
```

boot.py:`BootResult` 加字段 `scheduler: "FileSchedulerService | None" = None`(import 加 FileSchedulerService),boot() 返回时带上 Task 4 创建的 `scheduler`。

serve.py:boot 之后找到 adapters 启动处,先读 serve.py 确认注入文本进 session 的现有入口(adapters 把 InboundMessage 交给 gateway 的路径,grep `handle` / `gateway.`)。dispatch 回调实现为:构造 `AgentRequest(session_id=job_session, text=prompt, source="scheduler")` 并 `async for _ in gateway… `(消费完);如 gateway 有更直接的文本入口就用它。runner 在 adapters 启动后 `runner.start()`,进程退出路径 `runner.stop()`。**实现者必须先读 serve.py 全文再选注入点,把所选入口写进 commit message。**

- [ ] **Step 4: 测试 + 回归 + Commit**

```bash
git add -A src/xana/runtime/scheduler.py src/xana/runtime/boot.py src/xana/cli/serve.py tests/test_scheduler.py
git commit -m "feat(runtime): SchedulerRunner 到点把任务 prompt 经 gateway 注入 session"
```

---

### Task 6: web_search 工具(deferred;native/brave/duckduckgo)

**Files:**
- Create: `src/xana/tools/builtin/web_search.py`
- Test: `tests/test_web_search_tool.py`
- Modify: `src/xana/runtime/boot.py`(注册 + available_search_providers)
- Modify: `src/xana/llm_driver/providers/anthropic_deepseek.py`(native 注入)
- Modify: `src/xana/harness_sdk/default.py`(native 互斥)
- Modify: `pyproject.toml`(`ddgs`)

执行逻辑抄 `/home/xzp/project/yuxu_backup/src/yuxu/services/web_search/handler.py`(brave 305-354 行、duckduckgo 423-445 行、native 互斥 `_maybe_inject_web_search`)。

- [ ] **Step 1: 依赖**:pyproject dependencies 加 `"ddgs>=9.0"`(回退 import duckduckgo_search),`pip install ddgs`。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_web_search_tool.py
import pytest

from xana.policy.contracts import PermissionContext
from xana.session_config.contracts import SessionConfig
from xana.tools.builtin.web_search import WebSearchTool, _BRAVE_ENDPOINT
from xana.tools.contracts import ToolContext, ToolInvocation


class _CfgStore:
    def __init__(self, provider: str | None) -> None:
        self._p = provider

    async def get(self, session_id: str) -> SessionConfig:
        return SessionConfig(session_id=session_id, search_provider=self._p)


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", agent_name="chat", cwd="/tmp",
                       permission=PermissionContext(mode="default", session_id="s"))


def _inv(**inp) -> ToolInvocation:
    return ToolInvocation(tool_name="web_search", input={"query": "xana 是什么", **inp},
                          call_id="c", session_id="s")


def test_spec_is_deferred() -> None:
    assert WebSearchTool(session_config_store=_CfgStore(None)).spec().defer is True


@pytest.mark.asyncio
async def test_native_provider_refuses_function_call() -> None:
    # native = server 端 web_search_20250305,函数工具不该被调到;调到 → 引导性错误
    tool = WebSearchTool(session_config_store=_CfgStore(None))  # None/空 = native
    res = await tool.execute(_inv(), _ctx())
    assert not res.ok and res.failure_kind == "unsupported"
    assert "native" in (res.error or "")


@pytest.mark.asyncio
async def test_brave_dispatch_shapes_results(monkeypatch) -> None:
    tool = WebSearchTool(session_config_store=_CfgStore("brave"), brave_api_key="K")

    async def fake_get(url, *, headers, params):
        assert url == _BRAVE_ENDPOINT and headers["X-Subscription-Token"] == "K"
        return {"web": {"results": [
            {"title": "T1", "url": "https://a", "description": "D1"}]}}

    monkeypatch.setattr(tool, "_http_get_json", fake_get)
    res = await tool.execute(_inv(), _ctx())
    assert res.ok
    assert res.content["results"] == [{"title": "T1", "url": "https://a", "snippet": "D1"}]
    assert "T1" in res.rendered and "https://a" in res.rendered


@pytest.mark.asyncio
async def test_brave_without_key_falls_back_to_duckduckgo(monkeypatch) -> None:
    tool = WebSearchTool(session_config_store=_CfgStore("brave"), brave_api_key=None)
    monkeypatch.setattr(tool, "_duckduckgo",
                        lambda query, max_results: [{"title": "D", "url": "u", "snippet": "s"}])
    res = await tool.execute(_inv(), _ctx())
    assert res.ok and res.content["provider"] == "duckduckgo"
```

- [ ] **Step 3: 确认失败** → `python -m pytest tests/test_web_search_tool.py -x -q`

- [ ] **Step 4: 实现 `web_search.py`**

构造注入 `session_config_store`、`brave_api_key: str | None = None`。spec:`defer=True`、`open_world=True`、`read_only=True`,描述:"联网搜索。输入查询词,返回 {title, url, snippet} 结果列表。provider 由 /search_provider 会话设置决定(native/brave/duckduckgo)。",input_schema `{"query": {"type": "string", "description": "搜索查询词"}, "max_results": {"type": "number", "description": "最多返回条数(默认 10,上限 20)"}}` required ["query"]。

execute 分派(yuxu handler 同构):

```python
    async def execute(self, invocation, context):
        query = str(invocation.input.get("query", "")).strip()
        max_results = min(int(invocation.input.get("max_results", 10) or 10), 20)
        cfg = await self.session_config_store.get(context.session_id)
        provider = (cfg.search_provider or "native").lower()
        if provider == "native":
            return ToolResult(
                ok=False, content=None, failure_kind="unsupported",
                error="search_provider=native 时搜索由模型端 web_search server tool 完成,"
                      "函数工具不可用;用 /search_provider 切到 brave 或 duckduckgo。",
                rendered="[web_search] native 模式下请直接依赖模型内置搜索。")
        if provider == "brave" and self.brave_api_key:
            data = await self._http_get_json(
                _BRAVE_ENDPOINT, headers={"X-Subscription-Token": self.brave_api_key,
                                          "Accept-Encoding": "gzip"},
                params={"q": query, "count": max_results})
            results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                        "snippet": r.get("description", "")}
                       for r in data.get("web", {}).get("results", [])]
            used = "brave"
        else:
            results = await asyncio.to_thread(self._duckduckgo, query, max_results)
            used = "duckduckgo"
        rendered = "\n".join(f"- {r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results) \
                   or "(无结果)"
        return ToolResult(ok=True, content={"provider": used, "results": results},
                          rendered=rendered)
```

`_http_get_json` 用 httpx.AsyncClient(timeout 15s);`_duckduckgo` 抄 yuxu 423-445 行(`from ddgs import DDGS` 回退 `from duckduckgo_search import DDGS`,`DDGS().text(query, max_results=…)`,href→url、body→snippet)。

**native 注入(互斥规则,抄 yuxu `_maybe_inject_web_search`)**:
- `harness_sdk/default.py`:构造 ModelRequest 处,若 `config_snapshot.get("search_provider") in (None, "", "native")` 且 provider 以 "deepseek" 开头 → `tools_schema` 中剔除 name=="web_search" 的条目,并在 ModelRequest.metadata 加 `{"web_search_native": True}`(metadata 已是自由 dict)。
- `anthropic_deepseek.py`:读到 `request.metadata.get("web_search_native")` 时在请求体 tools 数组追加 `{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}`。先读该文件确认 payload 组装点。
- 测试(追加到 test_web_search_tool.py):构造 harness 级测试断言 native 时 tools_schema 不含 web_search 函数工具(参照 tests/test_harness_system_prompt.py 的 _run 惯用法,config 带 search_provider="native";FakeModelClient 捕获 request)。

boot.py:
```python
    await tools.register(WebSearchTool(
        session_config_store=session_config_store,
        brave_api_key=getattr(system_cfg, "brave_api_key", None)))
```
`available_search_providers=("bm25",)` 改为 `("native", "brave", "duckduckgo")`(/search_provider 命令即刻可切;bm25 是历史占位,无消费者,grep 确认后移除)。`brave_api_key` 进 system.json:读 `src/xana/config/loader.py` 的 SystemConfig,若是普通(非锁定)dataclass 加可选字段 `brave_api_key: str | None = None` 并在解析处透传;若 loader 归属 contracts 类锁定文件 → 改用环境变量 `BRAVE_API_KEY`(os.environ)并在计划 todo md 记录 setup 集成待做。

- [ ] **Step 5: 测试 + 回归 + Commit**

```bash
git add -A src/xana/tools/builtin/web_search.py src/xana/runtime/boot.py \
  src/xana/harness_sdk/default.py src/xana/llm_driver/providers/anthropic_deepseek.py \
  tests/test_web_search_tool.py pyproject.toml
git commit -m "feat(tools): web_search(native/brave/duckduckgo,provider 走 SessionConfig)"
```

---

### Task 7: web_fetch 工具(deferred)

**Files:**
- Create: `src/xana/tools/builtin/web_fetch.py`
- Test: `tests/test_web_fetch_tool.py`
- Modify: `src/xana/runtime/boot.py`、`pyproject.toml`(`beautifulsoup4`)

执行逻辑抄 yuxu `web_fetch/handler.py`:Chrome UA、redirect≤10、readable 抽取(bs4 去 script/style/nav/footer/header/aside/noscript/svg/form/iframe,无 bs4 回退正则去标签)、max_chars 截断(头+尾各半)。schema 按 yuxu(url/timeout/max_chars/extract),规格 §8 明示 web_fetch 参考 yuxu;CC 的 prompt+小模型加工进 todo md。

- [ ] **Step 1: 依赖**:pyproject 加 `"beautifulsoup4>=4.12"`,`pip install beautifulsoup4`。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_web_fetch_tool.py
import pytest

from xana.policy.contracts import PermissionContext
from xana.tools.builtin.web_fetch import WebFetchTool, _readable_text
from xana.tools.contracts import ToolContext, ToolInvocation


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", agent_name="chat", cwd="/tmp",
                       permission=PermissionContext(mode="default", session_id="s"))


def test_spec_is_deferred_and_readonly() -> None:
    spec = WebFetchTool().spec()
    assert spec.defer is True and spec.read_only is True


def test_readable_text_strips_chrome() -> None:
    html = ("<html><head><title>标题</title><style>x{}</style></head>"
            "<body><nav>导航</nav><p>正文第一段</p><script>evil()</script>"
            "<footer>页脚</footer></body></html>")
    text = _readable_text(html)
    assert "正文第一段" in text
    assert "导航" not in text and "evil" not in text and "页脚" not in text


@pytest.mark.asyncio
async def test_execute_fetches_and_truncates(monkeypatch) -> None:
    tool = WebFetchTool()

    async def fake_fetch(url, timeout):
        return ("https://final", 200, "text/html",
                "<html><title>T</title><body><p>" + "甲" * 50000 + "</p></body></html>")

    monkeypatch.setattr(tool, "_fetch", fake_fetch)
    inv = ToolInvocation(tool_name="web_fetch", call_id="c", session_id="s",
                         input={"url": "https://x", "max_chars": 1000})
    res = await tool.execute(inv, _ctx())
    assert res.ok and res.truncated
    assert res.content["title"] == "T" and res.content["status"] == 200
    assert len(res.content["text"]) <= 1100  # 截断标记略有富余


@pytest.mark.asyncio
async def test_execute_rejects_non_http() -> None:
    res = await WebFetchTool().execute(
        ToolInvocation(tool_name="web_fetch", call_id="c", session_id="s",
                       input={"url": "file:///etc/passwd"}), _ctx())
    assert not res.ok and res.failure_kind == "validation"
```

- [ ] **Step 3: 确认失败** → `python -m pytest tests/test_web_fetch_tool.py -x -q`

- [ ] **Step 4: 实现**

spec:`defer=True, read_only=True, open_world=True`,描述(CC 描述语义 + yuxu 行为的中文):"抓取指定 URL 的内容并抽取可读正文。返回标题、状态码与正文文本(超长截断,保留首尾)。URL 必须是完整合法的 http/https 地址;重定向自动跟随(至多 10 次)。本工具只读,不修改任何文件。",input_schema:url(string, required)、timeout(number, 默认 10 秒)、max_chars(number, 默认 20000)、extract(string enum ["readable","html","both"], 默认 readable)。

实现要点(yuxu handler 同构):`_fetch` 用 httpx.AsyncClient(follow_redirects=True, max_redirects=10, headers={"User-Agent": Chrome UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}) 返回 (final_url, status, content_type, text);`_readable_text(html)` 模块级函数,bs4 decompose 上列标签后 get_text("\n", strip=True),ImportError 回退 `re.sub(r"<(script|style)[^>]*>.*?</\1>", "", …, flags=S|I)` 再 `re.sub(r"<[^>]+>", " ", …)`;标题 `<title>` 正则;截断:`text[:max_chars//2] + "\n…(截断)…\n" + text[-max_chars//2:]` 当 len>max_chars,`truncated=True`;validate:url 必须 startswith http:// 或 https://(否则 execute 返回 validation 失败,如测试)。content = {"url": final_url, "status", "content_type", "title", "truncated", "text"(readable/both), "html"(html/both)};rendered = f"{title}\n{text}" 截断到 spec.max_result_chars。

boot 注册:`await tools.register(WebFetchTool())`。

- [ ] **Step 5: 测试 + 回归 + Commit**

```bash
git add -A src/xana/tools/builtin/web_fetch.py src/xana/runtime/boot.py tests/test_web_fetch_tool.py pyproject.toml
git commit -m "feat(tools): web_fetch 抓取+正文抽取(yuxu 同构,deferred)"
```

---

### Task 8: skill_load 工具 + 可用技能列表扩展 + 3 个 bundled skill

**Files:**
- Create: `src/xana/skills/bundled/__init__.py`、`loop.py`、`skill_creator.py`、`schedule.py`
- Create: `src/xana/tools/builtin/skill_load.py`
- Create: `src/xana/extension_api/builtin/skills_listing.py`(若 `extension_api/builtin/` 不存在则连 `__init__.py` 一起建)
- Test: `tests/test_skill_load.py`、`tests/test_bundled_skills.py`
- Modify: `src/xana/runtime/boot.py`

设计(用户已拍板 skill_load 方案):
- 每个 bundled skill 实现已有 `Skill` ABC:`spec()` 给 name/description/when_to_use;`activate()` 返回 `SkillContribution(prompt_blocks=(SKILL_BODY,))` —— **不会**被 harness 每轮注入,因为 bundled skill 注册进 SkillRegistry 后由 `discover(activation)` 控制;读 `src/xana/skills/registry.py` 确认 discover 语义:若 discover 返回全部已注册 skill(= 每轮全文注入),则 bundled skill 注册进**独立的** `InMemorySkillRegistry` 实例(`bundled_skills`),不挂在 harness 的 skill_registry 上,只供 skill_load 工具与列表扩展使用——零侵入。
- `SkillsListingContributor(TurnInputContributor)`(checklist §4 指定的 Contributor 类型):`contribute_turn_input` 返回一个 ContextFragment:「# 可用技能」+ 模板(AGENT_LOOP_PROMPT.md:136-140,"使用 skill_load 获取完整说明")逐 skill 一行 `- {name}: {description}`。构造注入 bundled_skills registry。经 `ExtensionRegistryBuilder().add(...)` 在 boot 组装(boot 是唯一组合点,现有 boot 的 extension_registry 参数若为 None 则 boot 内部新建 builder 只含此扩展;若调用方传入了 registry —— builder 不可变,改为 boot 新增逻辑:仅当 extension_registry is None 时才自建,否则原样用并把 bundled 列表能力放进 skill_load 工具即可,列表段缺失可接受,记 todo md)。
- `SkillLoadTool`:输入 `{"name": str}`,从 bundled_skills registry 找 skill,`activate()` 拿 prompt_blocks 全文返回 rendered。`defer=False`(直接可见——技能列表每轮在 prompt 里,load 工具必须随手可用)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_skill_load.py
import pytest

from xana.policy.contracts import PermissionContext
from xana.skills.bundled.loop import LoopSkill
from xana.skills.registry import InMemorySkillRegistry
from xana.tools.builtin.skill_load import SkillLoadTool
from xana.tools.contracts import ToolContext, ToolInvocation


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", agent_name="chat", cwd="/tmp",
                       permission=PermissionContext(mode="default", session_id="s"))


@pytest.mark.asyncio
async def test_load_returns_full_skill_body() -> None:
    reg = InMemorySkillRegistry()
    await reg.register(LoopSkill())
    tool = SkillLoadTool(skills=reg)
    res = await tool.execute(
        ToolInvocation(tool_name="skill_load", call_id="c", session_id="s",
                       input={"name": "loop"}), _ctx())
    assert res.ok
    assert "cron_create" in res.rendered  # loop skill 全文指导用 cron_create


@pytest.mark.asyncio
async def test_load_unknown_skill_lists_available() -> None:
    reg = InMemorySkillRegistry()
    await reg.register(LoopSkill())
    res = await SkillLoadTool(skills=reg).execute(
        ToolInvocation(tool_name="skill_load", call_id="c", session_id="s",
                       input={"name": "nope"}), _ctx())
    assert not res.ok and res.failure_kind == "not_found"
    assert "loop" in (res.rendered or "")
```

```python
# tests/test_bundled_skills.py
import pytest

from xana.skills.bundled.loop import LoopSkill
from xana.skills.bundled.schedule import ScheduleSkill
from xana.skills.bundled.skill_creator import SkillCreatorSkill


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_cls,name,anchors", [
    (LoopSkill, "loop", ("cron_create", "间隔", "立即执行一次")),
    (ScheduleSkill, "schedule", ("cron_create", "cron_list", "cron_delete", "确认")),
    (SkillCreatorSkill, "skill_creator", ("SKILL.md", "frontmatter", "成功标准")),
])
async def test_skill_spec_and_body(skill_cls, name, anchors) -> None:
    skill = skill_cls()
    assert skill.spec().component.name == name
    assert skill.spec().component.description
    from xana.skills.contracts import SkillActivation
    contrib = await skill.activate(SkillActivation(
        kind="model", session_id="s", agent_name="chat", run_id="r", cwd=None))
    body = "\n".join(contrib.prompt_blocks)
    for a in anchors:
        assert a in body, f"{name} 缺锚点 {a}"


def test_skills_listing_contributor_renders_template() -> None:
    import asyncio
    from xana.extension_api.builtin.skills_listing import SkillsListingContributor
    from xana.extension_api.contracts import TurnInputContext, ExtensionData
    from xana.skills.registry import InMemorySkillRegistry

    async def _go():
        reg = InMemorySkillRegistry()
        await reg.register(LoopSkill())
        c = SkillsListingContributor(skills=reg)
        frags = await c.contribute_turn_input(
            TurnInputContext(session_id="s", agent_name="chat", run_id="r", user_input="hi"),
            ExtensionData("s"), ExtensionData("r"))
        text = frags[0].content
        assert text.startswith("# 可用技能")
        assert "skill_load" in text and "- loop:" in text
    asyncio.run(_go())
```

- [ ] **Step 2: 确认失败** → `python -m pytest tests/test_skill_load.py tests/test_bundled_skills.py -x -q`

- [ ] **Step 3: 实现 bundled skills**

每个 skill 文件含一个 `_BODY` 常量(中文 SKILL 全文)+ Skill 实现(spec/activate 惯用法照抄旧 identity.py 的 SkillSpec 构造,activate 返回 `SkillContribution(prompt_blocks=(_BODY,))`)。skill 正文(CC bundled 内容的中文忠实改写,schedule 为本地适配版):

`loop.py` `_BODY`:

```
# loop — 周期性执行一个 prompt

用法:用户说「每 N 分钟/小时做 X」「循环检查 Y」时使用。

解析规则(按优先级):
1. 开头独立 token 形如 `10m`/`2h`/`1d` → 它是间隔,其余是 prompt
2. 末尾「每 N 分钟/小时/天」或 `every Nm` 子句 → 提取为间隔并从 prompt 删除
3. 都没有 → 默认间隔 10 分钟,整句是 prompt

间隔 → cron 转换:
- N 分钟(N≤59)→ `*/N * * * *`;N≥60 → 取整小时 `0 */H * * *`(H 须整除 24)
- N 小时(N≤23)→ `0 */N * * *`;N 天 → `0 0 */N * *`
- 秒级向上取整到 1 分钟(cron 最小粒度)
- 不整的间隔就近取整,并在调度前告知用户取整结果

执行步骤:
1. 调 cron_create(cron 表达式, prompt, recurring=true)
2. 用人话简短确认排期(频率 + 任务内容)
3. 立即执行一次解析出的 prompt——不等第一次 cron 触发
```

`schedule.py` `_BODY`(本地适配,保留 CC 交互流程):

```
# schedule — 管理定时任务(本地)

用法:用户要「定时/每天/提醒我/排个任务」时使用。任务由本机 serve 进程的调度器
执行,持久化在 ~/.xana/scheduled_tasks.json,serve 重启后仍有效。

第一步先弄清用户要做什么:创建 / 查看 / 修改 / 删除。

创建流程:
1. 理解目标:做什么、对哪个会话、成功标准是什么。prompt 要写得具体——
   触发时模型只看到这段 prompt,没有当前对话上下文。
2. 定时间:问清何时/多频繁,换算成本地时区的 5 字段 cron 表达式,
   把换算结果用人话复述给用户确认(如「每个工作日 9:03」)。
   避开 :00/:30 整点,选错峰分钟。
3. 一次性任务用 recurring=false(触发一次后自动删除);周期任务 recurring=true。
4. 用户确认后调 cron_create,把返回的任务 ID 告诉用户。

查看:cron_list 列出全部任务(ID/cron/prompt)。
修改:先 cron_delete 旧任务再 cron_create 新任务(无原地更新)。
删除:cron_delete(id)。删除前把要删的任务复述给用户确认。
```

`skill_creator.py` `_BODY`(CC skillify 的中文忠实版,访谈降级为普通提问——xana 暂无 request_user_input 工具):

```
# skill_creator — 把本次会话的可复用流程沉淀为 skill

用法:用户说「把刚才的流程存成 skill」「以后还要这么做」时使用。

第一步:分析会话
回顾本次会话:用户的目标是什么?执行了哪些步骤?哪些步骤每次都一样,
哪些会随参数变化?

第二步:访谈用户(逐轮提问,等回答再继续)
1. 名字与目标:建议一个 kebab-case 名字和一句话目标,请用户确认或修改
2. 步骤与参数:列出你识别的步骤,问哪些值应做成参数($arg 形式)
3. 每步细节:成功标准是什么?哪步要停下来等人确认?哪些步骤可并行?
4. 触发语:用户将来会怎么说来触发这个 skill?有什么坑要写进注意事项?

第三步:写 SKILL.md
格式(YAML frontmatter + 正文):
---
name: {skill-name}
description: {一句话描述}
when_to_use: {触发语和例子}
arguments: {参数名列表}
---
# {标题}
## 输入
- `$参数名`: 说明
## 目标
明确陈述目标与产出(成功标准)
## 步骤
### 1. 步骤名
做什么,要具体。
**成功标准**: 必填
(可选注记:产物 / 人工确认点 / 硬性规则;3a/3b 编号表示可并行)

第四步:把 SKILL.md 全文展示给用户确认,确认后写入用户指定的位置
(默认 ~/.xana/skills/{name}/SKILL.md)。
```

- [ ] **Step 4: 实现 skill_load 工具 + 列表扩展**

`skill_load.py`:spec defer=False、read_only=True,描述 "按名字加载一个可用技能的完整说明(SKILL 全文)。「# 可用技能」段列出的技能匹配当前任务时,先调本工具拿完整说明再执行。",input_schema {"name": {"type": "string", "description": "技能名,见「# 可用技能」列表"}} required ["name"]。execute:`skill = await self.skills.get(name)`(读 skills/registry.py 确认取单个的方法名,若只有 discover 则遍历匹配 spec().component.name);未找到 → not_found + rendered 列出全部可用名;找到 → activate(用 invocation/context 构造 SkillActivation(kind="model", session_id=context.session_id, agent_name=context.agent_name, run_id=context.run_id or "", cwd=Path(context.cwd))),rendered = "\n\n".join(contribution.prompt_blocks)。

`skills_listing.py`:

```python
class SkillsListingContributor(TurnInputContributor):
    """「# 可用技能」段(模板 AGENT_LOOP_PROMPT.md:136-140)。"""

    def __init__(self, *, skills) -> None:
        self._skills = skills

    async def contribute_turn_input(self, ctx, thread_store, turn_store):
        specs = await self._list_specs()
        if not specs:
            return []
        lines = "\n".join(f"- {s.component.name}: {s.component.description}" for s in specs)
        return [ContextFragment(content=(
            "# 可用技能\n\n"
            "以下技能已加载到此会话。使用 skill_load 获取完整说明。当技能匹配任务时使用它。\n\n"
            + lines), source="skills_listing")]
```

(`_list_specs` 按 registry 实际接口实现——先读 skills/registry.py。)

- [ ] **Step 5: boot 接线**

```python
    bundled_skills = InMemorySkillRegistry()
    for skill in (LoopSkill(), ScheduleSkill(), SkillCreatorSkill()):
        await bundled_skills.register(skill)
    await tools.register(SkillLoadTool(skills=bundled_skills))
    if extension_registry is None:
        from xana.extension_api.registry import ExtensionRegistryBuilder
        extension_registry = (ExtensionRegistryBuilder()
                              .add(SkillsListingContributor(skills=bundled_skills))
                              .build())
```

(放在 `DefaultHarness(extension_registry=extension_registry)` 构造之前;调用方已传 registry 时不强塞——builder frozen,该情形列表段缺失记入 todo md。)

- [ ] **Step 6: 测试 + 回归 + Commit**

Run: `python -m pytest tests/test_skill_load.py tests/test_bundled_skills.py -q && python -m pytest -q`

```bash
git add -A src/xana/skills/bundled/ src/xana/tools/builtin/skill_load.py \
  src/xana/extension_api/builtin/ src/xana/runtime/boot.py tests/
git commit -m "feat(skills): loop/schedule/skill_creator + skill_load 工具 + 可用技能段"
```

---

### Task 9: 待办汇总 todo md + 部署验证

**Files:**
- Create: `todo_ai/deferred_features.md`

- [ ] **Step 1: 写 `todo_ai/deferred_features.md`**

```markdown
# Tools/Skills 待办(依赖未完成功能,2026-06-13 计划遗留)

来源:ds_review/xana_review/tools-skills-checklist.md;已完成项见
docs/superpowers/plans/2026-06-13-tools-skills.md。

## 等待前置功能的项

- **request_user_input 工具**(CC AskUserQuestionTool):需要 adapter 层支持
  选项卡片渲染 + 回传选择(feishu/tg 交互消息)。做好后:skill_creator 的访谈
  改用本工具;schedule skill 的「创建/查看/修改/删除」首问也改用。
- **batch skill**(CC batch.ts):需要 worktree 隔离的后台子代理(AgentSpawner
  实现 + isolation="worktree")与 PR 工作流。前置:AgentSpawner 具体实现。
- **permission_check 系统级能力**:ToolLifecycleContributor 注入执行前权限
  检查,需要 runtime policy engine(政策规则来源 + 审批 UI)。
- **sandbox_run 系统级能力**:SandboxManager 具体实现(xana 无 sandbox,从零建,
  参考 Codex sandboxing/)。
- **schedule skill 的 remote 形态**(CC scheduleRemoteAgents):需要云端执行
  基础设施;当前为本地 cron 适配版。
- **web_fetch 的小模型加工**(CC 行为:prompt 参数 + Haiku 提炼):需要工具内
  二次 LLM 调用通道(构造注入 model_registry);当前返回抽取正文,由主模型自读。
- **compact 保留 revealed tools**:CC 在 compact boundary 记
  preCompactDiscoveredTools;xana 当前 compact 后 revealed 状态丢失,模型需
  重新 tool_search。做法:build_post_compact_context 透传 revealed 名单进
  boundary metadata,_revealed_tools 兼容读取。
- **xana setup 增加 search_provider/brave key 步骤**(若 Task 6 落地时走了
  环境变量回退):setup 向导写入 system.json。
- **boot 外部传入 extension_registry 时的「# 可用技能」段**:builder frozen,
  无法追加 SkillsListingContributor;需要 builder 合并能力或调用方自行加。
```

(实现者按 Task 6/8 的实际落地情况增删最后两条。)

- [ ] **Step 2: 全量测试**

Run: `python -m pytest -q`
Expected: 全部通过(已知 e2e 除外)

- [ ] **Step 3: 部署**

Run: `~/.xana/bin/xana sync`
Expected: serve 重启成功;`tail ~/.xana/logs/serve.log` 无报错。

- [ ] **Step 4: 人工抽查**

feishu/tg 发一条「用 tool_search 看看有什么隐藏工具」,确认模型能搜到 web_search/web_fetch/cron_*;发「列一下定时任务」确认 cron_list 走通。

- [ ] **Step 5: Commit(不 push)**

```bash
git add todo_ai/deferred_features.md
git commit -m "docs(todo): tools/skills 待办汇总(依赖未完成功能的项)"
```

---

## Self-Review 结论

- **Checklist 覆盖**:§1 tool_search(Task 2)✓;§2 deferred 五项:web_search(T6)、web_fetch(T7)、cron×3(T3-5)✓,enter_plan_mode/enter_worktree 已有(本期不改 defer 标记——它们已注册且无 schema 体积问题,如需转 deferred 一行 spec 改动,留给后续);§3 系统级两项 → todo md ✓;§4 skills:loop/skill_creator/schedule(T8)✓,batch → todo md ✓;§7 web_search 专项(native/brave/duckduckgo + 互斥 + /search_provider)✓;§8 约束(Extension/Tool ABC、boot 注册、deferred 机制、SchedulerService 注入、抄 yuxu)✓。
- **复用已有设计**:Tool 惯用法=enter_plan_mode.py;provider=SessionConfig.search_provider + 已有 /search_provider 命令;SchedulerService ABC 原样;Skill ABC + InMemorySkillRegistry 原样;TurnInputContributor/ExtensionRegistryBuilder 原样;revealed 走 MemoryMessage.metadata。
- **锁定文件**:唯一改动 ToolSpec.defer(已授权)。SchedulerService ABC 不动(pop_due 是实现类方法)。
- **占位检查**:无 TBD;两处「先读文件再定」(serve.py 注入点、SystemConfig 是否可加字段)是显式调查步骤并给出了两分支的完整做法。
```
