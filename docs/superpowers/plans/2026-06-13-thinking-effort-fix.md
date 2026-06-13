# thinking + effort DeepSeek API 对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `/home/xzp/project/best_agent/ds_review/xana_review/thinking-effort-fix.md` 列出的 8 个问题:effort 未传 API、reasoning_content 工具轮次丢失(400)、/effort 硬编码、/thinking 无 toggle、thinking 默认值、DSML 恢复不完整、compact 丢 revealed tools、tool_search raw XML 暴露到前端。

**Architecture:** 全部复用已有通道:effort 走已存在的 `ModelRequest.effort` 字段(harness 补传);reasoning_content 存 `MemoryMessage.metadata`(xana 既有设计),回放时在 provider 拼成 Anthropic thinking block(规格选项 A 的线格式 + B 的存储,用户已批准);DSML 刮除移植 yuxu `StreamingContextScrubber`;revealed tools 经 boundary metadata 过 compact;UI 覆盖用 `ToolResult.metadata["display_text"]` 通用键。锁定文件唯一授权改动:`SessionConfig.thinking` 默认 `False → True`(用户 2026-06-13 批准设计方案,规格 #5 明示)。

**Tech Stack:** Python 3.12, pytest-asyncio;无新依赖。

---

## 已拍板的设计(用户批准"按你推荐的")

1. **#2 reasoning_content**:存储用 metadata(`asst.metadata["reasoning_content"]`,harness 累积本回合 thinking deltas),线格式用 Anthropic blocks——`_build_request_body` 在**有 tool_calls 的 assistant 消息**前插 `{"type":"thinking","thinking":...}` block(DS 只要求工具轮次回传;无 tool_calls 轮保持纯文本)。
2. **#5 授权**:`session_config/contracts.py` 只改 `thinking: bool = False` → `= True` 这一处默认值;同时 harness 把 `config_snapshot["thinking"]`(bool)映射为 `request.metadata["thinking"]="enabled"/"disabled"`(compact 调用已显式 "disabled" 不受影响)。
3. **#3 supported_efforts**:provider 模块常量 `SUPPORTED_EFFORTS=("high","max")` + 兼容映射 `_EFFORT_MAP`(low/medium→high, xhigh→max);`DefaultCommandRouter` 加构造参数 `available_efforts`(boot 传入),不改任何 ABC。
4. **#6 DSML**:移植 yuxu `_stream_scrubber.py` 的 `StreamingContextScrubber`(文本 delta 实时刮除,raw 仍进 text_buffer 供恢复);恢复触发时打 `DSML_LEAK_WORKAROUND` WARNING;恢复 call_id 改为全局唯一(uuid 后缀)——现在的 `dsml-recovered-{idx}` 跨轮重复,多轮回放 tool_use id 冲突会 400。
5. **#7 revealed**:`harness_sdk/compact.py` 新增公共函数 `revealed_tools_from_messages`(逻辑自 default.py `_revealed_tools` 迁移,default.py 改为薄别名),`build_post_compact_context` 加 `revealed_tools` 参数写进 boundary metadata `pre_compact_revealed_tools`,扫描函数兼容读取;两个调用方(default.py auto-compact、commands.py /compact)都传。
6. **#8 display_text**:harness `tool_finished` 事件的 payload["text"] 在 `result.metadata["display_text"]` 存在时用它(模型上下文仍用 rendered);tool_search 设友好文案。

## 硬约束

- 除 `SessionConfig.thinking` 默认值一处外,不碰任何 `*/contracts.py` / `*/interfaces.py`。
- yuxu 参考:`/home/xzp/project/yuxu_backup/src/yuxu/_stream_scrubber.py`(StreamingContextScrubber 73-211 行)、`src/yuxu/services/llm_service/impls/_dsml.py`(recover_into_normalized 139-187 行,WARNING 文案)。
- 每任务全量 pytest;已知可忽略失败 `tests/test_xanac_e2e.py::test_xanac_empty_input_exits_nonzero`;Pyright xana.* 导入报错是陈旧误报。
- 本地 commit,不 push;最后 `~/.xana/bin/xana sync`。

## File Structure

```
Modify: src/xana/llm_driver/providers/anthropic_deepseek.py  (effort/thinking块/scrubber/唯一id/WARNING)
Create: src/xana/llm_driver/providers/stream_scrubber.py     (yuxu 移植)
Modify: src/xana/harness_sdk/default.py                      (effort 传递/thinking metadata/reasoning 累积/display_text/compact revealed)
Modify: src/xana/harness_sdk/compact.py                      (revealed_tools_from_messages + boundary metadata)
Modify: src/xana/agent_adapter/commands.py                   (/effort 动态、/thinking //streaming toggle、/compact 传 revealed)
Modify: src/xana/session_config/contracts.py                 (仅 thinking 默认值,已授权)
Modify: src/xana/runtime/boot.py                             (available_efforts 传入)
Tests:  tests/test_effort_plumbing.py, tests/test_reasoning_replay.py,
        tests/test_dsml_scrubber.py, tests/test_command_toggles.py,
        追加 tests/test_compact_functions.py / test_deferred_tools.py / test_command_compact.py
```

---

### Task 1: effort 链路(#1 + #3)

**Files:**
- Modify: `src/xana/harness_sdk/default.py`(ModelRequest 构造处,~475 行)
- Modify: `src/xana/llm_driver/providers/anthropic_deepseek.py`(`_build_request_body`)
- Modify: `src/xana/agent_adapter/commands.py:205-210`(`_cmd_effort`)+ 构造函数
- Modify: `src/xana/runtime/boot.py`(command_router 构造)
- Test: `tests/test_effort_plumbing.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_effort_plumbing.py
"""effort: HarnessContext.effort → ModelRequest.effort → DS output_config.effort。"""
from __future__ import annotations

import pytest

from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.providers.anthropic_deepseek import (
    SUPPORTED_EFFORTS, AnthropicDeepSeekConfig, _build_request_body,
)


def _req(effort=None) -> ModelRequest:
    return ModelRequest(provider="deepseek", model="m", effort=effort,
                        messages=(ModelMessage(role="user", content="hi"),),
                        tools_schema=())


def test_supported_efforts_constant() -> None:
    assert SUPPORTED_EFFORTS == ("high", "max")


@pytest.mark.parametrize("effort,wire", [
    ("high", "high"), ("max", "max"),
    ("low", "high"), ("medium", "high"), ("xhigh", "max"),  # 兼容映射
])
def test_effort_mapped_into_output_config(effort, wire) -> None:
    body = _build_request_body(_req(effort), AnthropicDeepSeekConfig(api_key="k"))
    assert body["output_config"] == {"effort": wire}


def test_no_effort_no_output_config() -> None:
    body = _build_request_body(_req(None), AnthropicDeepSeekConfig(api_key="k"))
    assert "output_config" not in body


@pytest.mark.asyncio
async def test_harness_passes_context_effort_to_model_request() -> None:
    # 复用 tests/test_harness_system_prompt.py 的 _run 惯用法,spy client 捕获 request;
    # HarnessContext(effort="max") → captured.effort == "max"
    ...


@pytest.mark.asyncio
async def test_cmd_effort_validates_against_available_and_lists() -> None:
    from xana.agent_adapter.commands import DefaultCommandRouter
    from xana.agent_adapter.contracts import InboundMessage
    from xana.memory.active_context import InMemoryActiveContextStore

    router = DefaultCommandRouter(active_context_store=InMemoryActiveContextStore(),
                                  available_efforts=("high", "max"))
    inbound = InboundMessage(adapter_id="t", session_id="s", user_id="u", text="/effort")
    res = await router.route_command(inbound)
    assert res.ok and "high" in res.message and "max" in res.message  # 无参=列出
    bad = InboundMessage(adapter_id="t", session_id="s", user_id="u", text="/effort low")
    res2 = await router.route_command(bad)
    assert not res2.ok and "high" in res2.message  # 不在列表 → 报错并给出可选值
```

(harness 测试与 router 构造按现有测试惯用法补全——先读 tests/test_harness_system_prompt.py 与 tests/test_command_compact.py;`_persist_with` 需要 session_config_store,无则用 stub。)

- [ ] **Step 2: 确认失败** → `python -m pytest tests/test_effort_plumbing.py -x -q`

- [ ] **Step 3: 实现**

anthropic_deepseek.py 模块级:

```python
SUPPORTED_EFFORTS = ("high", "max")  # DS API 实际支持;low/medium/xhigh 是兼容映射
_EFFORT_MAP = {"low": "high", "medium": "high", "high": "high",
               "xhigh": "max", "max": "max"}
```

`_build_request_body` 在 thinking 开关之后:

```python
    # DS API: {"output_config": {"effort": "high"|"max"}};兼容值就近映射
    effort = _EFFORT_MAP.get(str(request.effort).lower()) if request.effort else None
    if effort:
        body["output_config"] = {"effort": effort}
```

default.py ModelRequest 构造加 `effort=context.effort,`。

commands.py:构造函数加 `available_efforts: tuple[str, ...] = ("high", "max")`(存 self);`_cmd_effort` 重写:

```python
    async def _cmd_effort(self, inbound: InboundMessage, args: str) -> CommandResult:
        target = args.strip().lower()
        opts = ", ".join(self.available_efforts)
        if not target:
            return CommandResult(ok=True, message=f"可选 effort: {opts}")
        if target not in self.available_efforts:
            return CommandResult(ok=False, message=f"effort 必须为 {opts}")
        new = await self._persist_with(inbound.session_id, effort=target)
        return CommandResult(ok=True, message=f"effort 切换为 {new.effort}")
```

boot.py command_router 构造加 `available_efforts=SUPPORTED_EFFORTS,`(import 自 anthropic_deepseek)。

- [ ] **Step 4: 测试 + 全量回归** → `python -m pytest tests/test_effort_plumbing.py -q && python -m pytest -q`(旧 /effort 测试若断言 low/medium 合法,更新为新语义)

- [ ] **Step 5: Commit** → `feat(effort): effort 传入 DS output_config;/effort 动态校验 provider 支持值`

---

### Task 2: thinking 默认值 + metadata 贯通(#5)

**Files:**
- Modify: `src/xana/session_config/contracts.py:20`(仅默认值,已授权)
- Modify: `src/xana/harness_sdk/default.py`(request_metadata)
- Test: 追加 `tests/test_effort_plumbing.py` 或新建 `tests/test_thinking_default.py`

- [ ] **Step 1: 写失败测试**

```python
def test_session_config_thinking_defaults_true() -> None:
    from xana.session_config.contracts import SessionConfig
    assert SessionConfig(session_id="s").thinking is True


@pytest.mark.asyncio
async def test_harness_threads_thinking_metadata() -> None:
    # spy client;config_snapshot {"thinking": True} → request.metadata["thinking"]=="enabled"
    # config_snapshot {"thinking": False} → "disabled";缺省(无 key)→ 不设(保持原行为)
    ...
```

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

contracts.py: `thinking: bool = True  # DS API 默认 thinking enabled,与之对齐(2026-06-13 授权)`。

default.py 构造 request_metadata 处:

```python
        if "thinking" in context.config_snapshot:
            request_metadata.setdefault(
                "thinking",
                "enabled" if context.config_snapshot.get("thinking") else "disabled")
```

- [ ] **Step 4: 测试 + 回归**(受影响旧测试:断言 thinking 默认 False 的,改 True)

- [ ] **Step 5: Commit** → `feat(thinking): SessionConfig.thinking 默认 True(已授权)并贯通到请求 metadata`

---

### Task 3: reasoning_content 工具轮次回放(#2,P0)

**Files:**
- Modify: `src/xana/harness_sdk/default.py`(流循环累积 thinking;assistant metadata)
- Modify: `src/xana/llm_driver/providers/anthropic_deepseek.py`(`_build_request_body` assistant 分支)
- Test: `tests/test_reasoning_replay.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reasoning_replay.py
"""DS 要求:工具调用轮次的 assistant 消息,后续轮次必须回传 reasoning_content,否则 400。
存储:assistant MemoryMessage.metadata["reasoning_content"];
线格式:Anthropic blocks,thinking block 在最前(仅有 tool_calls 的轮)。"""
from __future__ import annotations

from xana.llm_driver.contracts import ModelMessage, ModelRequest
from xana.llm_driver.providers.anthropic_deepseek import (
    AnthropicDeepSeekConfig, _build_request_body,
)


def _body(messages) -> dict:
    req = ModelRequest(provider="d", model="m", messages=tuple(messages), tools_schema=())
    return _build_request_body(req, AnthropicDeepSeekConfig(api_key="k"))


def test_tool_call_turn_replays_thinking_block_first() -> None:
    msgs = [
        ModelMessage(role="user", content="做事"),
        ModelMessage(role="assistant", content="好",
                     metadata={"tool_calls": [{"call_id": "c1", "tool_name": "bash",
                                               "input": {}}],
                               "reasoning_content": "我想想"}),
        ModelMessage(role="tool", content="结果", metadata={"tool_use_id": "c1"}),
    ]
    blocks = _body(msgs)["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "我想想"}
    assert blocks[1]["type"] == "text" and blocks[2]["type"] == "tool_use"


def test_text_only_turn_stays_plain_string() -> None:
    msgs = [ModelMessage(role="user", content="hi"),
            ModelMessage(role="assistant", content="答",
                         metadata={"reasoning_content": "想"})]
    assert _body(msgs)["messages"][1]["content"] == "答"  # 无 tool_calls → 不回传(DS 只要求工具轮)


def test_tool_call_turn_without_reasoning_unchanged() -> None:
    msgs = [ModelMessage(role="user", content="x"),
            ModelMessage(role="assistant", content="",
                         metadata={"tool_calls": [{"call_id": "c1", "tool_name": "t",
                                                   "input": {}}]})]
    blocks = _body(msgs)["messages"][1]["content"]
    assert blocks[0]["type"] == "tool_use"  # 没 reasoning 不插空块
```

再加 harness 级测试(同文件):FakeModelClient 脚本含 thinking channel delta + tool_call(参照 tests/test_thinking_channel.py 现有惯用法),断言落库的 assistant MemoryMessage `metadata["reasoning_content"] == 思考全文` 且 content 不含思考。

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

default.py 流循环:`iter_text` 旁加 `iter_thinking: list[str]`,thinking channel 的 delta 进它(现在是直接丢弃,只 yield);构造 asst_metadata 时:

```python
            if iter_thinking:
                asst_metadata["reasoning_content"] = "".join(iter_thinking)
```

anthropic_deepseek.py `_build_request_body` assistant 有 tool_calls 分支,blocks 组装最前插:

```python
                reasoning = (msg.metadata or {}).get("reasoning_content")
                if reasoning:
                    blocks.append({"type": "thinking", "thinking": reasoning})
```

(在 text block 之前。)

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `fix(llm): 工具轮次回放 reasoning_content thinking block(DS 400 修复)`

---

### Task 4: /thinking /streaming 无参数 toggle(#4)

**Files:**
- Modify: `src/xana/agent_adapter/commands.py:212-224`
- Test: `tests/test_command_toggles.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_command_toggles.py — 统一 toggle 规范:无参=取反,on/off=显式
# router 构造照抄 tests/test_command_compact.py 的 _router(需 session_config_store,
# 用 FileSessionConfigStore(tmp_path) 或现有内存 stub)
@pytest.mark.asyncio
@pytest.mark.parametrize("cmd,field,default", [
    ("/thinking", "thinking", True), ("/streaming", "streaming", True),
])
async def test_no_arg_toggles(cmd, field, default, tmp_path) -> None:
    router, store = _router_with_config(tmp_path)
    res = await router.route_command(_inbound(cmd))
    assert res.ok
    assert getattr(await store.get("s1"), field) is (not default)
    res2 = await router.route_command(_inbound(cmd))
    assert getattr(await store.get("s1"), field) is default


@pytest.mark.asyncio
async def test_explicit_on_off_still_work(tmp_path) -> None:
    router, store = _router_with_config(tmp_path)
    await router.route_command(_inbound("/thinking off"))
    assert (await store.get("s1")).thinking is False
    await router.route_command(_inbound("/thinking on"))
    assert (await store.get("s1")).thinking is True


@pytest.mark.asyncio
async def test_bad_arg_rejected(tmp_path) -> None:
    router, _ = _router_with_config(tmp_path)
    res = await router.route_command(_inbound("/streaming maybe"))
    assert not res.ok
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**(两个 handler 同构改写)

```python
    async def _cmd_thinking(self, inbound: InboundMessage, args: str) -> CommandResult:
        val = args.strip().lower()
        if val and val not in {"on", "off"}:
            return CommandResult(ok=False, message="thinking 用法: /thinking [on|off](无参数=切换)")
        if val:
            new_val = val == "on"
        else:
            current = await self.session_config_store.get(inbound.session_id)
            new_val = not current.thinking
        new = await self._persist_with(inbound.session_id, thinking=new_val)
        return CommandResult(ok=True, message=f"thinking={new.thinking}")
```

(`_cmd_streaming` 同形;CommandSpec description 更新为 "…开关(无参数切换)"。)

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `feat(commands): /thinking /streaming 无参数 toggle(统一规范)`

---

### Task 5: DSML 流式刮除 + 唯一 id + WARNING(#6,P0/P1)

**Files:**
- Create: `src/xana/llm_driver/providers/stream_scrubber.py`(yuxu `_stream_scrubber.py:73-211` 移植,API: feed/flush/reset)
- Modify: `src/xana/llm_driver/providers/dsml_recovery.py`(确认/补 DSML_FENCE_TAGS 导出)
- Modify: `src/xana/llm_driver/providers/anthropic_deepseek.py`(`_parse_sse_stream` 接线)
- Test: `tests/test_dsml_scrubber.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dsml_scrubber.py
from xana.llm_driver.providers.stream_scrubber import StreamingContextScrubber
from xana.llm_driver.providers.dsml_recovery import DSML_FENCE_TAGS


def test_scrubs_complete_span_single_delta() -> None:
    s = StreamingContextScrubber(DSML_FENCE_TAGS)
    out = s.feed("前文<｜DSML｜tool_calls>内部</｜DSML｜tool_calls>后文")
    assert out + s.flush() == "前文后文"


def test_partial_tag_across_deltas_held_back() -> None:
    s = StreamingContextScrubber(DSML_FENCE_TAGS)
    a = s.feed("答案<｜DS")          # 可能是 tag 开头 → 扣住
    b = s.feed("ML｜tool_calls>x</｜DSML｜tool_calls>尾")
    assert a + b + s.flush() == "答案尾"


def test_false_alarm_tail_emitted_on_flush() -> None:
    s = StreamingContextScrubber(DSML_FENCE_TAGS)
    out = s.feed("结尾是<｜")
    assert out + s.flush() == "结尾是<｜"


def test_unterminated_span_dropped() -> None:
    s = StreamingContextScrubber(DSML_FENCE_TAGS)
    out = s.feed("好<｜DSML｜tool_calls>没闭合")
    assert out + s.flush() == "好"
```

再加 provider 级测试(放同文件或追加现有 anthropic_deepseek 测试文件):构造含 DSML 泄漏的 SSE 流(参照现有 dsml 测试),断言 (a) text_delta 事件拼起来不含 DSML 标记;(b) 仍 yield 恢复的 tool_call;(c) 两次泄漏的 call_id 互不相同且非固定 "dsml-recovered-0";(d) caplog 里有 "DSML_LEAK_WORKAROUND"。

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

1. `stream_scrubber.py`:整类移植 yuxu(feed 的状态机:span 内找闭合、span 外找最早开标签、`_max_partial_suffix` 扣尾、flush 未闭合即丢弃),docstring 注明来源。
2. `dsml_recovery.py`:导出 `DSML_FENCE_TAGS`(若已有等价物则 re-export 统一名)。
3. `_parse_sse_stream`:函数开头 `scrubber = StreamingContextScrubber(DSML_FENCE_TAGS)`;`_dispatch_sse_event` 的 text_delta 分支改为——raw text 照旧进 `text_buffer`(恢复扫描要原文),但 yield 的事件文本换成 `scrubber.feed(text)`,空则不 yield;SSE 结束后 `tail = scrubber.flush()`,非空则补发一个 text_delta;恢复段:

```python
    recovered_text = "".join(text_buffer)
    _, leaked = parse_dsml_tool_calls(recovered_text)
    if leaked:
        _logger.warning(
            "DSML_LEAK_WORKAROUND deepseek endpoint leaked %d tool call(s) as raw "
            "DSML; recovered client-side: %s", len(leaked), [c["name"] for c in leaked])
    for call in leaked:
        yield ModelEvent(kind="tool_call", payload={
            "tool_name": call["name"], "input": call["input"],
            "call_id": f"{_DSML_RECOVERED_CALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"})
```

(模块加 `import logging, uuid` 与 `_logger`。注意:`_dispatch_sse_event` 需要拿到 scrubber——把它作为参数传入,签名内部函数自由改。)

cleaned_text 说明:harness 落库的 assistant content 来自 yield 出的(已刮除)delta,所以存储与 UI 自动干净——不需要单独替换 content;此与 yuxu 的 recover_into_normalized 等效(在 commit message 里写明该等效性)。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `fix(llm): DSML 流式刮除(yuxu 移植)+ 恢复 call_id 全局唯一 + DSML_LEAK_WORKAROUND 监控`

---

### Task 6: compact 保留 revealed tools(#7)

**Files:**
- Modify: `src/xana/harness_sdk/compact.py`(新公共函数 + build_post_compact_context 参数)
- Modify: `src/xana/harness_sdk/default.py`(`_revealed_tools` 改薄别名;auto-compact 传参)
- Modify: `src/xana/agent_adapter/commands.py`(`_cmd_compact` 传参)
- Test: 追加 `tests/test_compact_functions.py`、`tests/test_deferred_tools.py`、`tests/test_command_compact.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加 tests/test_compact_functions.py
def test_build_post_compact_context_carries_revealed_tools() -> None:
    ctx = build_post_compact_context(session_id="s", agent_name="chat", summary="S",
                                     revealed_tools=("web_search", "cron_create"))
    assert ctx.messages[0].metadata["pre_compact_revealed_tools"] == ["cron_create", "web_search"]


def test_revealed_tools_from_messages_reads_boundary_metadata() -> None:
    from xana.harness_sdk.compact import revealed_tools_from_messages
    msgs = (_m("system", "对话已压缩", compact_boundary=True,
               pre_compact_revealed_tools=["web_search"]),
            _m("tool", "x", tool_name="tool_search", revealed_tools=["cron_list"]))
    assert revealed_tools_from_messages(msgs) == {"web_search", "cron_list"}
```

```python
# 追加 tests/test_command_compact.py:/compact 后 revealed 不丢
# 先 append 一条带 metadata revealed_tools=["web_search"] 的 tool 消息再 /compact,
# 断言压缩后 snapshot.messages[0].metadata["pre_compact_revealed_tools"] == ["web_search"]
```

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

compact.py:

```python
def revealed_tools_from_messages(messages) -> set[str]:
    """已揭示工具 = 消息 metadata.revealed_tools ∪ compact boundary 的
    pre_compact_revealed_tools(CC: preCompactDiscoveredTools 同思路)。"""
    out: set[str] = set()
    for m in messages:
        for key in ("revealed_tools", "pre_compact_revealed_tools"):
            names = m.metadata.get(key)
            if names:
                out.update(names)
    return out
```

`build_post_compact_context(*, session_id, agent_name, summary, revealed_tools: Sequence[str] = ())`:boundary metadata 变为 `{"compact_boundary": True}`,revealed 非空时加 `"pre_compact_revealed_tools": sorted(revealed_tools)`。

default.py:`_revealed_tools = revealed_tools_from_messages`(import 别名,删本地实现);auto-compact 调用处先 `revealed = revealed_tools_from_messages(live)`(live 是 boundary 之后的消息,boundary 自己的 pre_compact 列表也要并上——直接对 `history_msgs` 全量扫即可),传 `revealed_tools=sorted(revealed)`。

commands.py `_cmd_compact`:writeback 前 `revealed = revealed_tools_from_messages(snap.messages)`,传入。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `fix(compact): boundary 透传 pre_compact_revealed_tools,压缩不再丢已发现工具`

---

### Task 7: tool_search 前端友好显示(#8)+ 部署

**Files:**
- Modify: `src/xana/harness_sdk/default.py`(tool_finished payload)
- Modify: `src/xana/tools/builtin/tool_search.py`(metadata display_text)
- Test: 追加 `tests/test_tool_search.py` / `tests/test_deferred_tools.py`

- [ ] **Step 1: 写失败测试**

```python
# 追加 tests/test_tool_search.py
@pytest.mark.asyncio
async def test_result_has_friendly_display_text() -> None:
    tool = ToolSearchTool(tool_registry=await _registry())
    res = await tool.execute(_inv("select:web_search"), _ctx())
    assert "<functions>" in res.rendered            # 模型上下文保持完整 schema
    disp = res.metadata["display_text"]
    assert "<functions>" not in disp and "web_search" in disp
```

```python
# 追加 harness 级断言(tests/test_deferred_tools.py 的 e2e 里):
# 收集 harness yield 的 tool_finished 事件,tool_search 那条 payload["text"] 不含 "<functions>",
# 而落库的 tool MemoryMessage content 仍含 "<functions>"(模型可见)
```

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

tool_search.py execute 返回前:

```python
        display = (f"已加载 {len(revealed)} 个工具: {', '.join(revealed)},下一轮可直接调用。"
                   if revealed else rendered)  # 未命中时的纯文本本来就友好
        # metadata 同时携带 revealed_tools(harness 持久化)与 display_text(UI 覆盖)
```

metadata 加 `"display_text": display`。

default.py `tool_finished` yield 处:

```python
                display_text = result.metadata.get("display_text") or rendered
```

payload["text"] 用 `display_text`(MemoryMessage 仍存 rendered——模型上下文不变)。

- [ ] **Step 4: 全量测试** → `python -m pytest -q`
- [ ] **Step 5: Commit + 部署**

```bash
git commit -m "fix(tools): tool_search 前端显示友好文案,模型上下文保留完整 schema"
~/.xana/bin/xana sync
```

抽查:feishu/tg 发"用 tool_search 找 web_search",前端不出现 raw `<functions>`;`/effort` 无参列出 high/max;`/thinking` 无参切换;日志 grep DSML_LEAK_WORKAROUND 观测。

---

## Self-Review 结论

- **规格覆盖**:#1→T1,#2→T3,#3→T1,#4→T4,#5→T2,#6→T5,#7→T6,#8→T7,全部有任务;优先级 P0(#2/#6/#1)在 T1/T3/T5。
- **锁定文件**:唯一改动 SessionConfig.thinking 默认值(已授权);supported_efforts 用模块常量+构造参数,不动 ABC。
- **占位检查**:两处 harness 级测试标 `...` 并指明了照抄的现有测试文件(test_harness_system_prompt.py / test_thinking_channel.py 惯用法),属显式调查步骤;其余代码完整。
- **一致性**:`reasoning_content` 键名在 T3 的 harness 与 provider 两端一致;`pre_compact_revealed_tools` 在 T6 三处一致;`display_text` 在 T7 两端一致。
