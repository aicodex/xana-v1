# Prompt Fix(系统提示对齐 CC)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `/home/xzp/project/best_agent/ds_review/xana_review/prompt-fix.md` 把 xana 的系统提示替换为 CC 2.1.88 的 13 段结构:7 个静态中文 section(常量)+ 动态拼装(环境/工具/隐藏工具/技能/代理/记忆),全部 prompt 必须接入(暂无对应功能的段抄原文 + TODO 注释,函数挂上但返回空串)。

**Architecture:** 新建 `src/xana/harness_sdk/system_prompt.py` 存 7 个静态常量(逐字来自 `ds_review/ROADMAP/AGENT_LOOP_PROMPT.md`)+ 动态段构建函数 + `build_system_prompt()` 纯函数。`DefaultHarness._invoke` 用它替换现有 base_parts 拼接;动态中段沿用现有 `PromptAssembler`(ContextContributor fragments)与 memory/skill blocks,不区分来源直接拼。`IdentitySkill` 的静态块全部退役(与新常量冲突且含被 spec 严禁的内容)。Agent 级覆盖通过 `config_snapshot["agent_system_prompt"]` / `["agent_identity"]` 传入——**不改任何锁定的 contracts/interfaces 文件**。

**Tech Stack:** Python 3.12, pytest(asyncio), 现有 xana 三层架构(contracts/interfaces/implementation),`boot()` 唯一组合点。

---

## 规格来源(实现者必读,均为只读参考)

| 文件 | 用途 |
|---|---|
| `/home/xzp/project/best_agent/ds_review/xana_review/prompt-fix.md` | 主规格:三层结构、TODO 抄原文规则、四条严禁 |
| `/home/xzp/project/best_agent/ds_review/ROADMAP/AGENT_LOOP_PROMPT.md` | 7 个静态 section + 5 个动态段模板的**逐字中文翻译**(本计划给出精确行号) |
| `/home/xzp/project/best_agent/ds_review/ROADMAP/PROMPT_ASSEMBLY_ORDER.md` | 13 段拼装顺序、CLAUDE.md 放 messages[0] 的说明、Agent 覆盖语义 |

### 严禁(来自 prompt-fix.md,CI 之外的人工红线)

- ❌ 不加 CC 没有的东西(不要 "clean refactor of yuxu"、不要 "don't summarize what you just did")
- ❌ 不删 CC 有的东西——xana 暂无该功能也要抄原文 + TODO
- ❌ 不在一个文件写死完整 prompt——静态常量与动态拼装分离
- ❌ 不在 prompt 里区分"系统的/用户的"来源——直接拼在一起

### ⚠️ 接口变更授权状态

- ✅ **已授权(2026-06-13 用户口头批准)**:`AgentSpec`(`src/xana/agents/contracts.py`)加 `system_prompt: str | None = None` 与 `identity: str = ""` 两个字段 → Task 4b 执行。除这两个字段外不得改该文件任何其他内容。
- ❌ **未授权**:`ToolSpec.defer`(`src/xana/tools/contracts.py`,deferred tools 机制实现时再申请),本计划不碰。

### 13 段最终顺序(= `build_system_prompt` 的输出顺序)

```
 1. INTRO(+ agent identity 追加,若有)   静态
 2. SYSTEM                               静态
 3. DOING_TASKS                          静态
 4. ACTIONS                              静态
 5. 环境信息 env_section()                动态,harness 每 turn 拼
 6. 工具列表 tools_section()              动态(当前返回 ""——schema 走 API tools 参数,CC 同款)
 7. 隐藏工具 deferred_tools_section()     动态(当前返回 ""——TODO(deferred_tools))
 8-10. 技能/代理/记忆 = dynamic_middle    现有 memory blocks + skill blocks + extension fragments(PromptAssembler)
11. USING_TOOLS                          静态
12. TONE                                 静态
13. OUTPUT                               静态
```

空段不产生多余空行(join 前过滤空串)。Agent 覆盖语义:override 非空 → 跳过全部静态段(1-4、11-13),只保留 override + 动态段(5-10)。

---

## File Structure

- Create: `src/xana/harness_sdk/system_prompt.py` — 7 静态常量 + `env_section` / `tools_section` / `deferred_tools_section` / `build_system_prompt`
- Create: `tests/test_system_prompt.py` — 常量锚点 + 拼装顺序/覆盖语义
- Modify: `src/xana/harness_sdk/default.py` — `_invoke` 中 system_content 的组装(约 325-363 行)
- Modify: `src/xana/runtime/boot.py:51,134` — 移除 IdentitySkill 注册
- Delete: `src/xana/skills/builtin/identity.py` 及其专属测试
- Modify: 受影响的现有 harness 测试(断言旧英文 prompt 的)

---

### Task 1: 静态常量文件 system_prompt.py

**Files:**
- Create: `src/xana/harness_sdk/system_prompt.py`
- Test: `tests/test_system_prompt.py`

- [ ] **Step 1: 写失败测试(关键锚点,来自 AGENT_LOOP_PROMPT.md "对照 CC 的关键提示"表)**

```python
# tests/test_system_prompt.py
from xana.harness_sdk import system_prompt as sp


def test_static_sections_exist_and_are_chinese() -> None:
    for name in ("INTRO", "SYSTEM", "DOING_TASKS", "ACTIONS",
                 "USING_TOOLS", "TONE", "OUTPUT"):
        text = getattr(sp, name)
        assert isinstance(text, str) and text.strip()


def test_critical_anchors_not_lost_in_translation() -> None:
    # 最容易被遗漏/改写的 7 个片段(见 AGENT_LOOP_PROMPT.md:175-187)
    assert "不受上下文窗口的限制" in sp.SYSTEM            # "not limited by the context window"
    assert "这至关重要" in sp.USING_TOOLS                  # "This is CRITICAL"
    assert "不要添加超出被要求范围的功能" in sp.DOING_TASKS
    assert "三个相似的代码行比过早的抽象好" in sp.DOING_TASKS
    assert "上传内容到第三方网络工具" in sp.ACTIONS
    assert "不意味着批准所有场景" in sp.ACTIONS
    assert "量两次，切一次" in sp.ACTIONS


def test_forbidden_content_absent() -> None:
    all_text = "\n".join(getattr(sp, n) for n in (
        "INTRO", "SYSTEM", "DOING_TASKS", "ACTIONS", "USING_TOOLS", "TONE", "OUTPUT"))
    assert "yuxu" not in all_text
    assert "refactor" not in all_text.lower()


def test_unimplemented_features_kept_verbatim() -> None:
    # CC 有、xana 暂无的功能,prompt 原文必须在(prompt-fix.md "加 TODO 抄原文")
    assert "钩子" in sp.SYSTEM            # hooks
    assert "todo_write" in sp.USING_TOOLS  # TodoWrite 工具
    assert "/help" in sp.DOING_TASKS
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/xzp/project/xana && python -m pytest tests/test_system_prompt.py -x -q`
Expected: FAIL `ModuleNotFoundError`/`AttributeError`

- [ ] **Step 3: 写常量文件**

创建 `src/xana/harness_sdk/system_prompt.py`。7 个常量的正文**逐字拷贝** `/home/xzp/project/best_agent/ds_review/ROADMAP/AGENT_LOOP_PROMPT.md` 的代码块(下表行号为该文件当前行号;拷贝 ``` 围栏内的全部内容,一个字不改):

| 常量 | 源行号 | 第一行锚点 |
|---|---|---|
| `INTRO` | 15-18 | `你是 {agent_name}。…`(见下方替换说明) |
| `SYSTEM` | 24-31 | `# 系统` |
| `DOING_TASKS` | 37-50 | `# 执行任务` |
| `ACTIONS` | 56-66 | `# 谨慎执行操作` |
| `USING_TOOLS` | 72-82 | `# 使用工具` |
| `TONE` | 88-94 | `# 语气与风格` |
| `OUTPUT` | 100-111 | `# 输出效率` |

文件骨架(正文用 `"""\ ... """` 三引号字符串;`<完整拷贝…>` 处放逐字原文,这是唯一允许的"占位",因为源是规范文件):

```python
"""CC 2.1.88 系统提示中文版 — 静态常量 + 动态段构建。

7 个静态 section 逐字来自 ds_review/ROADMAP/AGENT_LOOP_PROMPT.md。
这些字符串永远不变,不在代码里拼接;拼装见 build_system_prompt()。
CC 源文件: claude-code-2.1.88-src/constants/prompts.ts。
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

INTRO = """\
你是 xana——一个自主编码助手。使用下方说明和可用工具来帮助用户。

<完整拷贝 AGENT_LOOP_PROMPT.md:17-18 两段「重要：」>
"""

# TODO(hooks): hook 系统实现后,本段的钩子行自动生效(现在就抄原文,不删)
SYSTEM = """\
<完整拷贝 24-31>
"""

DOING_TASKS = """\
<完整拷贝 37-50>
"""

ACTIONS = """\
<完整拷贝 56-66>
"""

# TODO(todo_write): TodoWrite 工具实现后,本段 todo_write 行自动生效
USING_TOOLS = """\
<完整拷贝 72-82>
"""

TONE = """\
<完整拷贝 88-94>
"""

OUTPUT = """\
<完整拷贝 100-111>
"""

STATIC_HEAD = (INTRO, SYSTEM, DOING_TASKS, ACTIONS)
STATIC_TAIL = (USING_TOOLS, TONE, OUTPUT)
```

注意:INTRO 模板首行 `你是 {agent_name}。` 按 prompt-fix.md:18 固化为 `你是 xana——一个自主编码助手。`,其余两段「重要：」逐字保留。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/test_system_prompt.py -x -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/system_prompt.py tests/test_system_prompt.py
git commit -m "feat(prompt): CC 7 段静态系统提示中文常量(逐字 AGENT_LOOP_PROMPT.md)"
```

---

### Task 2: 动态段构建 + build_system_prompt 纯函数

**Files:**
- Modify: `src/xana/harness_sdk/system_prompt.py`(追加)
- Test: `tests/test_system_prompt.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_system_prompt.py

def test_env_section_template() -> None:
    out = sp.env_section(cwd="/tmp/x", model="claude-x", model_display="Claude X")
    assert out.startswith("# 环境\n")
    assert "- 主工作目录: /tmp/x" in out
    assert "- Git 仓库: " in out
    assert "- 平台: " in out
    assert "- Shell: " in out
    assert "- OS 版本: " in out
    assert "你使用的模型是 Claude X。模型 ID 为 claude-x。" in out


def test_tools_and_deferred_sections_are_empty_for_now() -> None:
    # schema 走 API tools 参数(CC 同款); deferred tools 机制未实现 → 返回空串
    assert sp.tools_section() == ""
    assert sp.deferred_tools_section() == ""


def test_build_system_prompt_order() -> None:
    out = sp.build_system_prompt(
        identity="你是测试代理。",
        env="# 环境\n- x",
        dynamic_middle=("# 记忆\n- m1", "# 可用技能\n- s1"),
    )
    idx = [out.index(s) for s in (
        "你是 xana", "你是测试代理。", "# 系统", "# 执行任务", "# 谨慎执行操作",
        "# 环境", "# 记忆", "# 可用技能", "# 使用工具", "# 语气与风格", "# 输出效率")]
    assert idx == sorted(idx), "13 段顺序错误"


def test_build_system_prompt_override_skips_statics_keeps_dynamics() -> None:
    out = sp.build_system_prompt(
        override="你是一个创意写作助手。",
        env="# 环境\n- x",
        dynamic_middle=("# 记忆\n- m1",),
    )
    assert out.startswith("你是一个创意写作助手。")
    assert "# 环境" in out and "# 记忆" in out      # 动态段永远注入
    assert "# 执行任务" not in out and "# 输出效率" not in out  # 静态段全部跳过


def test_build_system_prompt_no_blank_segments() -> None:
    out = sp.build_system_prompt(env="", dynamic_middle=())
    assert "\n\n\n" not in out
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_system_prompt.py -x -q`
Expected: FAIL `AttributeError: env_section`

- [ ] **Step 3: 实现(追加到 system_prompt.py)**

```python
def env_section(*, cwd: str | None, model: str, model_display: str | None = None) -> str:
    """# 环境 段,模板逐字 AGENT_LOOP_PROMPT.md:123-130;每 turn 由 harness 拼。"""
    cwd_str = cwd or "(unknown)"
    is_git = bool(cwd) and (Path(cwd) / ".git").exists()
    display = model_display or model
    return (
        "# 环境\n\n"
        f"- 主工作目录: {cwd_str}\n"
        f"- Git 仓库: {'true' if is_git else 'false'}\n"
        f"- 平台: {platform.system().lower() or 'unknown'}\n"
        f"- Shell: {os.environ.get('SHELL', 'unknown')}\n"
        f"- OS 版本: {platform.platform()}\n"
        f"- 你使用的模型是 {display}。模型 ID 为 {model}。"
    )


def tools_section() -> str:
    # 工具完整 schema 走 ModelRequest.tools_schema(API tools 参数),CC 同款 —
    # prompt 第 6 段在 xana 中恒为空,函数保留以固定 13 段拼装位。
    return ""


# TODO(deferred_tools): 隐藏工具机制(ToolSpec.defer + tool_search)实现后,
# 这里返回「# 按需工具」列表(模板 AGENT_LOOP_PROMPT.md:166-170)。
# 当前返回空字符串,不影响已有功能。
def deferred_tools_section() -> str:
    return ""


def build_system_prompt(
    *,
    identity: str = "",
    override: str | None = None,
    env: str = "",
    dynamic_middle: tuple[str, ...] = (),
) -> str:
    """13 段拼装(PROMPT_ASSEMBLY_ORDER.md)。override 非空 → 跳过全部静态段,
    动态段(env/工具/技能/记忆)永远注入。系统与 extension 注入的内容直接拼在
    一起,不区分来源。"""
    if override:
        parts: list[str] = [override]
    else:
        parts = [INTRO]
        if identity:
            parts.append(identity)
        parts += [SYSTEM, DOING_TASKS, ACTIONS]
    parts += [env, tools_section(), deferred_tools_section(), *dynamic_middle]
    if not override:
        parts += list(STATIC_TAIL)
    return "\n\n".join(p for p in parts if p)
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/test_system_prompt.py -x -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/xana/harness_sdk/system_prompt.py tests/test_system_prompt.py
git commit -m "feat(prompt): 动态段构建 + build_system_prompt 13 段拼装纯函数"
```

---

### Task 3: DefaultHarness 接入(替换 base_parts 拼接)

**Files:**
- Modify: `src/xana/harness_sdk/default.py`(`_invoke` 约 325-363 行的 system_content 组装)
- Test: `tests/test_harness_system_prompt.py`(新建)

现状(default.py:357-363):

```python
base_parts = injected_head + skill_blocks + injected_tail + extension_tail
if ext is not None and ext.context:
    system_content = await ExtensionPromptAssembler(ext).assemble(
        thread_store=thread_store, turn_store=turn_store, base_parts=base_parts)
else:
    system_content = "\n\n".join(p for p in base_parts if p)
```

目标:base_parts(memory/skill/extension 注入)成为 13 段中的**动态中段**(第 8-10 位),外面包上静态段与环境段。PromptAssembler 的 slot 排序逻辑原样保留(只用已有接口)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_harness_system_prompt.py
"""DefaultHarness 的 system message 必须是 13 段 CC 结构。"""
from __future__ import annotations

import pytest

from xana.extension_api.contracts import PromptFragment
from xana.extension_api.interfaces import ContextContributor
from xana.extension_api.registry import ExtensionRegistryBuilder
from xana.harness_sdk.contracts import HarnessContext, HarnessRequest
from xana.harness_sdk.default import DefaultHarness
from xana.llm_driver.fake_client import FakeModelClient, InMemoryModelRegistry, ScriptedTurn
from xana.memory.active_context import InMemoryActiveContextStore
from xana.memory.history import InMemoryHistoryMirror


async def _run(harness: DefaultHarness, *, config: dict | None = None) -> str:
    store = InMemoryActiveContextStore()
    reg = InMemoryModelRegistry()
    await reg.register("fake", FakeModelClient(ScriptedTurn(text_deltas=("ok",))))
    ctx = HarnessContext(
        active_context_store=store,
        history_mirror=InMemoryHistoryMirror(),
        model_registry=reg,
        model="fake-m",
        cwd="/tmp",
        config_snapshot={"provider": "fake", **(config or {})},
    )
    req = HarnessRequest(session_id="s1", agent_name="chat", user_input="hi")
    async for _ in harness.invoke(req, ctx):
        pass
    assert harness.last_assembled_messages is not None
    sys_msg = harness.last_assembled_messages[0]
    assert sys_msg.role == "system"
    return sys_msg.content


@pytest.mark.asyncio
async def test_system_prompt_has_cc_13_segment_structure() -> None:
    content = await _run(DefaultHarness())
    assert content.startswith("你是 xana")
    idx = [content.index(s) for s in (
        "# 系统", "# 执行任务", "# 谨慎执行操作", "# 环境",
        "# 使用工具", "# 语气与风格", "# 输出效率")]
    assert idx == sorted(idx)
    assert content.rstrip().endswith(("。", "."))  # 以 OUTPUT 段结尾,无尾随空段
    assert "# 输出效率" in content.rsplit("# 语气与风格", 1)[1]


@pytest.mark.asyncio
async def test_env_section_reflects_context() -> None:
    content = await _run(DefaultHarness())
    assert "- 主工作目录: /tmp" in content
    assert "模型 ID 为 fake-m" in content


@pytest.mark.asyncio
async def test_extension_fragments_land_in_dynamic_middle() -> None:
    class _Mem(ContextContributor):
        async def contribute_prompt(self, thread_store, turn_store):
            return [PromptFragment(slot="system", content="# 记忆\n- 条目甲")]

    registry = ExtensionRegistryBuilder().add(_Mem()).build()
    content = await _run(DefaultHarness(extension_registry=registry))
    # 动态中段:在 ACTIONS(# 谨慎执行操作)之后、USING_TOOLS(# 使用工具)之前
    assert content.index("# 谨慎执行操作") < content.index("# 记忆") < content.index("# 使用工具")


@pytest.mark.asyncio
async def test_no_legacy_english_prompt() -> None:
    content = await _run(DefaultHarness())
    assert "yuxu" not in content
    assert "Tone and style" not in content
```

注:`ExtensionRegistryBuilder` 的 add 方式以 `src/xana/extension_api/registry.py` 实际签名为准(先读该文件);`InMemoryHistoryMirror` 名字以 `src/xana/memory/history*.py` 实际导出为准。HarnessContext 必填字段以 `harness_sdk/contracts.py` 为准——若有 abort/run_id 等必填项,照现有测试(如 `tests/test_harness_*.py`)的构造方式补齐。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_harness_system_prompt.py -x -q`
Expected: FAIL(现 prompt 无 "# 系统" 等段)

- [ ] **Step 3: 改 default.py**

在 import 区追加:

```python
from xana.harness_sdk.system_prompt import build_system_prompt, env_section
```

把 357-363 行替换为:

```python
        base_parts = injected_head + skill_blocks + injected_tail + extension_tail
        if ext is not None and ext.context:
            dynamic_middle_text = await ExtensionPromptAssembler(ext).assemble(
                thread_store=thread_store, turn_store=turn_store, base_parts=base_parts,
            )
        else:
            dynamic_middle_text = "\n\n".join(p for p in base_parts if p)
        # TODO(claude_md): CC 把 CLAUDE.md 包 <system-reminder> 作为 messages[0]
        # prepend(PROMPT_ASSEMBLY_ORDER.md),不进 system prompt;xana 实现
        # 项目级规则文件加载后在这里 prepend 首条 user message。
        system_content = build_system_prompt(
            identity=str(context.config_snapshot.get("agent_identity", "") or ""),
            override=context.config_snapshot.get("agent_system_prompt") or None,
            env=env_section(cwd=context.cwd or None, model=context.model),
            dynamic_middle=(dynamic_middle_text,),
        )
```

- [ ] **Step 4: 运行新测试 + 全量回归**

Run: `python -m pytest tests/test_harness_system_prompt.py -q && python -m pytest -q`
Expected: 新测试通过;回归中**预期失败**的是断言旧英文 prompt 的既有测试(下一步修)和已知的 `tests/test_xanac_e2e.py::test_xanac_empty_input_exits_nonzero`(预先存在,忽略)。

- [ ] **Step 5: 修受影响的既有测试**

Run: `grep -rln "Tone and style\|clean refactor\|# Environment\|prompt_blocks" tests/ | sort`
对每个命中文件:把断言旧 IdentitySkill 文案的改为断言新段落锚点(如 `"# 语气与风格" in content`)。只改断言文本,不改测试结构。

Run: `python -m pytest -q`
Expected: 除已知 e2e 外全部通过

- [ ] **Step 6: Commit**

```bash
git add src/xana/harness_sdk/default.py tests/
git commit -m "feat(harness): system prompt 改为 CC 13 段拼装(静态常量+动态中段)"
```

---

### Task 4: Agent 级覆盖语义(config_snapshot 通道)

**Files:**
- Test: `tests/test_harness_system_prompt.py`(追加)
- Modify: 无(Task 3 已实现读取;本任务补语义测试,锁死行为)

- [ ] **Step 1: 写失败前先确认现状**

Task 3 已让 harness 读 `config_snapshot["agent_system_prompt"]` / `["agent_identity"]`。本任务用测试锁死 PROMPT_ASSEMBLY_ORDER.md "Agent 层面" 的两条语义。

```python
# 追加到 tests/test_harness_system_prompt.py

@pytest.mark.asyncio
async def test_agent_identity_appended_after_intro() -> None:
    content = await _run(DefaultHarness(), config={"agent_identity": "你是甲代理。"})
    assert content.index("你是 xana") < content.index("你是甲代理。") < content.index("# 系统")


@pytest.mark.asyncio
async def test_agent_system_prompt_override_replaces_statics_only() -> None:
    content = await _run(
        DefaultHarness(), config={"agent_system_prompt": "你是一个创意写作助手。"})
    assert content.startswith("你是一个创意写作助手。")
    assert "# 环境" in content                # 动态段照常注入
    assert "# 执行任务" not in content        # 静态段全部跳过
    assert "# 输出效率" not in content
```

- [ ] **Step 2: 运行**

Run: `python -m pytest tests/test_harness_system_prompt.py -q`
Expected: PASS(若 FAIL 说明 Task 3 的 build_system_prompt 接线有误,在 default.py/system_prompt.py 修复后重跑)

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness_system_prompt.py
git commit -m "test(harness): 锁死 agent identity 追加与 system_prompt 全量覆盖语义"
```

---

### Task 4b: AgentSpec 字段接入(已授权的锁定文件改动)

**Files:**
- Modify: `src/xana/agents/contracts.py:21-37`(AgentSpec — 只加两个字段,其余一字不动)
- Modify: `src/xana/agents/chat_agent.py:107-117`(`to_harness_context` 的 config_snapshot)
- Test: `tests/test_harness_system_prompt.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_harness_system_prompt.py

@pytest.mark.asyncio
async def test_agent_spec_prompt_fields_flow_into_config_snapshot() -> None:
    from xana.agents.chat_agent import DefaultAgentRequestMapper, build_chat_agent_spec
    from xana.agents.contracts import AgentRequest
    from dataclasses import replace

    spec = replace(build_chat_agent_spec(),
                   identity="你是甲代理。", system_prompt=None)
    mapper = _make_mapper()  # 按本文件/现有 tests 中 DefaultAgentRequestMapper 的构造方式建一个最小 mapper
    ctx = await mapper.to_harness_context(
        spec, AgentRequest(session_id="s1", text="hi", source="test"))
    assert ctx.config_snapshot["agent_identity"] == "你是甲代理。"
    assert ctx.config_snapshot.get("agent_system_prompt") is None

    spec2 = replace(spec, system_prompt="你是写作助手。")
    ctx2 = await mapper.to_harness_context(
        spec2, AgentRequest(session_id="s1", text="hi", source="test"))
    assert ctx2.config_snapshot["agent_system_prompt"] == "你是写作助手。"
```

`_make_mapper()`:参照现有 agent 测试(`grep -rln DefaultAgentRequestMapper tests/`)构造,所有依赖用对应 InMemory 实现。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_harness_system_prompt.py -q`
Expected: FAIL(`AgentSpec` 无 system_prompt 字段)

- [ ] **Step 3: 实现**

`agents/contracts.py` 的 `AgentSpec` 字段区末尾(`max_turns` 之后)追加两行,带注释:

```python
    # None → 完整默认 13 段系统提示;非空 → 替换全部静态段,动态段照常注入
    system_prompt: str | None = None
    # Agent 身份,追加在 INTRO 之后(仅 system_prompt=None 时生效)
    identity: str = ""
```

`chat_agent.py` `to_harness_context` 的 config_snapshot 字面量追加:

```python
            "agent_identity": spec.identity,
            "agent_system_prompt": spec.system_prompt,
```

- [ ] **Step 4: 运行测试 + 全量回归**

Run: `python -m pytest tests/test_harness_system_prompt.py -q && python -m pytest -q`
Expected: 通过(已知 e2e 除外)

- [ ] **Step 5: Commit**

```bash
git add src/xana/agents/contracts.py src/xana/agents/chat_agent.py tests/test_harness_system_prompt.py
git commit -m "feat(agents): AgentSpec.system_prompt/identity 字段(已授权)接入 prompt 拼装"
```

---

### Task 5: 退役 IdentitySkill

**Files:**
- Modify: `src/xana/runtime/boot.py:51,134`
- Delete: `src/xana/skills/builtin/identity.py`
- Modify/Delete: 引用它的测试

旧 prompt 与新静态段重复注入且含 spec 严禁内容("clean refactor of yuxu"、"Don't summarize what you just did"),必须整体移除。

- [ ] **Step 1: 找全部引用**

Run: `grep -rln "IdentitySkill\|builtin.identity\|identity_text" src/ tests/`
Expected: `src/xana/runtime/boot.py`、`src/xana/skills/builtin/identity.py`、若干测试文件

- [ ] **Step 2: 移除注册与文件**

- `boot.py`:删除第 51 行 `from xana.skills.builtin.identity import IdentitySkill` 与第 134 行 `await skills.register(IdentitySkill())`
- `git rm src/xana/skills/builtin/identity.py`
- 专测 IdentitySkill 的测试文件整体删除;混合测试只删相关用例

- [ ] **Step 3: 全量回归**

Run: `python -m pytest -q`
Expected: 除已知 e2e 外全部通过

- [ ] **Step 4: Commit**

```bash
git add -A src/xana/runtime/boot.py src/xana/skills tests/
git commit -m "refactor(prompt): 退役 IdentitySkill,系统提示统一由 system_prompt.py 提供"
```

---

### Task 6: 部署验证

- [ ] **Step 1: 全量测试**

Run: `python -m pytest -q`
Expected: 全部通过(已知 e2e 除外)

- [ ] **Step 2: 同步部署(自动重启 serve)**

Run: `~/.xana/bin/xana sync`
Expected: sync 成功,serve 重启

- [ ] **Step 3: 人工抽查**

向 feishu/tg 任一会话发一条消息,确认回复正常、无 prompt 相关报错;可在日志确认 system message 以 "你是 xana" 开头。

- [ ] **Step 4: 不要 push**

仅本地 commit。远端 off-limits(用户长期指令)。

---

## Self-Review 结论

- **Spec 覆盖:** 三层结构(常量文件/Task 1;动态拼装/Task 2-3;deferred tools/`deferred_tools_section` TODO 占位)✓;TODO 抄原文(hooks、todo_write、deferred_tools、claude_md 四处 TODO)✓;Agent 级控制(Task 4,锁定文件改动留授权)✓;四条严禁(test_forbidden_content_absent + 不分来源直接拼 + 常量/拼装分离)✓;13 段顺序(test_build_system_prompt_order + harness 测试)✓。
- **未实现而留 TODO 的:** deferred tools 机制(ToolSpec.defer + tool_search)、TodoWrite 工具、hooks 系统、CLAUDE.md→messages[0]。prompt 原文全部接入,功能后补时各段自动生效。
- **锁定文件:** 本计划零改动 `*/contracts.py`、`*/interfaces.py`。AgentSpec/ToolSpec 字段为待授权项,见计划顶部。
