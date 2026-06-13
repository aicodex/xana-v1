# 命令卡片抽象 + 按钮 + TG 渲染修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ① 所有 slash 命令的结果走"新卡片":发送瞬间创建卡片,完成后在同卡片填结果(复用 compact 那套 pre-create + patch);② 按钮能力抽象(send 型 = 点击等于发送文本,copy 型 = 点击把文本放进输入/剪贴板),给 /model /search_provider /effort 加选项按钮、/help(新增 /h 别名)加 copy 按钮、compact 卡加"取消"按钮(停流 + 不写回);③ 修 TG:工具卡只有一个折叠块(去 `<pre>` 嵌套),全局禁用链接预览(yuxu 同款)。

**Architecture:** 零锁定文件改动。卡片预建走现有 `ProgressEvent`/`AdapterProgressSink` 通道:gateway 在路由命令前给命令分配 `run_id=cmd-<session>-<seq>` 并发一个 `command_progress` 事件(adapters 的 `note_progress` 已支持按 run_id 预建 turn,只需放宽 kind 判断);命令完成后 gateway 把 `run_id` 放进 AgentEvent payload,adapters 按 run_id 找到同一卡片 finalize。按钮:`CommandResult.payload["buttons"]` = `[[{"text","send"} | {"text","copy"}]]`,feishu/tg 的回调→InboundMessage 链路已存在(feishu.py:204 `_on_card_action`,telegram.py:295 `_handle_callback_query`),只补 copy 型(TG 用 Bot API `copy_text` 按钮;feishu 调研原生 copy 行为,无则回退 send)。compact 取消:router 持 `{run_id: asyncio.Event}`,隐藏命令 `/compact_cancel <run_id>` 置事件,`generate_summary` 的 on_progress 里检测到即中止、不写回。

**Tech Stack:** Python 3.12, pytest-asyncio;无新依赖。参考:yuxu_backup `services/gateway/adapters/telegram.py:157-166,652-707`(link_preview + 单折叠工具块)。

---

## 现状锚点(实现者必读)

- gateway 命令拦截:`src/xana/agent_adapter/gateway.py:47-69` — CommandResult → `AgentEvent(kind="final"/"error", payload={"text": msg, **payload})` → `adapter.render()` 一次。**问题:无 run_id,adapter 找不到/复用旧 turn。**
- compact 预建:`feishu.py:349-375` / `telegram.py:487-513` `note_progress` — `kind=="compact_progress"` 且带 run_id 时 `_ensure_turn(buf, run_id)` 预建;coalescer.flush patch 卡片。
- 按钮已有:`feishu_card.py:76-99` `_buttons_elements`(callback value `{"send_text"}`);`feishu.py:204-226` `_on_card_action` → InboundMessage;`telegram_html.py:78-83` `buttons_to_reply_markup`(callback_data `btn:r:c`);`telegram.py:295-347` `_handle_callback_query` + `_resolve_button_text`(从 `ts.last_buttons` 反查);`extract_buttons(content)` 解析正文内按钮标记(模型可用,保留)。
- TG 工具卡:`telegram_html.py:38-48` `render_tool_block` — `<pre>` 嵌在 `<blockquote expandable>` 内 → TG 拆成两块;yuxu 正确版 `yuxu_backup/.../telegram.py:652-707`:纯转义文本 + `\n────────\n` 分隔,单 blockquote,无 pre。
- TG 链接预览:`telegram_client.py` send/edit 均未设 `link_preview_options`;yuxu `telegram.py:165-166,237-238`:`body["link_preview_options"] = {"is_disabled": True}`(send 与 edit 都加)。
- contracts(锁定,只读):CommandResult.payload 自由 dict;ProgressEvent payload 自由 dict;Adapter ABC 不动。

---

### Task 1: 通用命令卡片(预建 + 同卡 finalize)

**Files:**
- Modify: `src/xana/agent_adapter/gateway.py:47-69`
- Modify: `src/xana/agent_adapter/channels/feishu.py`(note_progress kind 判断 + final 按 payload run_id 找 turn)
- Modify: `src/xana/agent_adapter/channels/telegram.py`(同上)
- Test: `tests/test_command_card.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_command_card.py
"""每个 slash 命令 = 一张新卡:gateway 发 command_progress 预建,final 落同卡。"""
import pytest


@pytest.mark.asyncio
async def test_gateway_emits_command_progress_then_final_with_same_run_id():
    # 构造 DefaultAgentGateway + stub adapter(记录 note_progress 与 render 调用)
    # + DefaultCommandRouter(仅 /help 即可)。照抄 tests/test_default_gateway.py 的搭建。
    # 断言:
    #   1) adapter.note_progress 先被调,payload kind=="command_progress",
    #      run_id 形如 "cmd-<session>-<n>"
    #   2) 随后 adapter.render 收到的 AgentEvent.payload["run_id"] == 同一 run_id
    #   3) 连续两次命令 run_id 不同(每命令新卡)
    ...


@pytest.mark.asyncio
async def test_compact_keeps_its_own_run_id():
    # /compact 自己发 compactcmd-… 进度;gateway 不得再叠一层新卡:
    # 路由前 gateway 已发 command_progress(cmd-…),compact 内部再发 compactcmd-…
    # → 会出两张卡。解法:gateway 把分配的 run_id 通过 inbound.metadata["command_run_id"]
    # 传给 router,_cmd_compact 改用它(替换自产 compactcmd-…)。断言 /compact 全程只一个 run_id。
    ...
```

(测试细节按 tests/test_default_gateway.py / tests/test_command_compact.py 现有惯用法补全;stub adapter 实现 note_progress/render 两个方法即可。)

- [ ] **Step 2: 确认失败**

- [ ] **Step 3: 实现**

gateway.py 命令路径改为:

```python
            self._cmd_seq += 1
            run_id = f"cmd-{inbound.session_id}-{self._cmd_seq}"
            await self._notify_command_card(adapter, inbound, run_id)  # 预建卡
            enriched = dataclasses.replace(
                inbound, metadata={**inbound.metadata, "command_run_id": run_id})
            cmd_result = await router.route_command(enriched)
            ...
            event = AgentEvent(kind=..., payload={"text": ..., "run_id": run_id, **cmd_result.payload})
```

`_notify_command_card`:调 `adapter.note_progress({"kind": "command_progress", "run_id": run_id, "session_id": inbound.session_id, "text": f"⏳ {inbound.text.split()[0]}"})`(hasattr 守卫,console 等无此方法的 adapter 跳过)。

feishu.py / telegram.py `note_progress`:预建判断 `payload.get("kind") not in ("compact_progress", "command_progress")` 才 return;command_progress 的 text 进 `ts.draft.content`。

feishu.py / telegram.py 的 final/error 渲染路径:AgentEvent payload 带 `run_id` 时 `_ensure_turn(buf, run_id)` 取那张卡(而不是当前/最新 turn)。读两个 render 方法现状后落点自定,保持 harness 事件路径不变。

commands.py `_cmd_compact`:`run_id = inbound.metadata.get("command_run_id") or f"compactcmd-…"`(兼容直接调用的旧测试)。

- [ ] **Step 4: 测试 + 全量回归**(旧测试若断言 compactcmd- 前缀,放宽为"单一 run_id")
- [ ] **Step 5: Commit** → `feat(adapter): 每个命令一张新卡(command_progress 预建 + run_id 贯通 finalize)`

---

### Task 2: 按钮抽象(payload buttons + copy 型)

**Files:**
- Modify: `src/xana/agent_adapter/channels/feishu.py`(final 渲染读 payload["buttons"])+ `feishu_card.py`(copy 型调研/回退)
- Modify: `src/xana/agent_adapter/channels/telegram.py` + `telegram_html.py`(copy_text 按钮)
- Test: `tests/test_command_card.py`(追加)

按钮统一形状:`[[{"text": "标签", "send": "点击即发送的文本"} | {"text": "标签", "copy": "点击放进输入/剪贴板的文本"}]]`。

- [ ] **Step 1: 写失败测试**

```python
def test_tg_reply_markup_supports_copy_buttons():
    from xana.agent_adapter.channels.telegram_html import buttons_to_reply_markup
    rows = [[{"text": "发送", "send": "/model x"}, {"text": "复制", "copy": "/help"}]]
    markup = buttons_to_reply_markup(rows)
    kb = markup["inline_keyboard"][0]
    assert kb[0] == {"text": "发送", "callback_data": "btn:0:0"}
    assert kb[1] == {"text": "复制", "copy_text": {"text": "/help"}}  # Bot API copyTextButton
```

```python
# feishu: _buttons_elements 对 copy 型生成 copy 行为(若飞书 card 2.0 支持
# behaviors type=copy/copy_text;实现者先查 feishu_card.py 现状与飞书文档/yuxu;
# 不支持则回退 callback send,测试断言回退行为并注释原因)
```

```python
@pytest.mark.asyncio
async def test_final_event_buttons_land_on_card():
    # CommandResult.payload["buttons"] = [[{"text":"a","send":"/x"}]] →
    # adapter 渲染 final 时 ts.draft.buttons == payload 里的 rows
    # (tg 还要 ts.last_buttons 同步,callback 反查才有效 — telegram.py:327)
    ...
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

- telegram_html.py `buttons_to_reply_markup`:row 内有 `copy` 键 → `{"text": …, "copy_text": {"text": copy值[:256]}}`(无 callback_data,TG 客户端本地处理,无需回调);`send` 键照旧 `btn:r:c`。`_resolve_button_text` 对 copy 按钮天然不会命中(没 callback)。
- feishu final 渲染(feishu.py:283-304 附近):`payload.get("buttons")` 合法时 `ts.draft.buttons = payload["buttons"]`(优先于 extract_buttons 的结果,两者可并存:payload 行在前)。telegram 同位置同改 + `ts.last_buttons` 同步。
- feishu_card.py `_buttons_elements`:`copy` 键 → 飞书行为调研:卡片 2.0 button `behaviors` 是否有 `{"type":"copy_text"}` 或 `callback` 之外的复制行为;有则用,没有则回退 `{"type":"callback","value":{"send_text": copy值}}` 并在代码注释里写明回退原因。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `feat(adapter): 按钮抽象 payload buttons(send/copy 两型,tg copy_text)`

---

### Task 3: 菜单接按钮(/model /search_provider /effort /help+/h)

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`
- Test: `tests/test_command_menus.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_command_menus.py(router 搭建照抄 test_command_compact.py)
@pytest.mark.asyncio
async def test_model_menu_has_option_buttons():
    router = _router(available_models=("ds-v4", "ds-r2"))
    res = await router.route_command(_inbound("/model"))
    assert res.ok
    rows = res.payload["buttons"]
    flat = [b for row in rows for b in row]
    assert {"text": "ds-v4", "send": "/model ds-v4"} in flat
    assert {"text": "ds-r2", "send": "/model ds-r2"} in flat


@pytest.mark.asyncio
async def test_search_provider_and_effort_menus_have_buttons():
    # 同型断言:/search_provider → send "/search_provider native" 等;
    # /effort → send "/effort high" / "/effort max"
    ...


@pytest.mark.asyncio
async def test_help_buttons_are_copy_type_and_h_alias():
    router = _router()
    res = await router.route_command(_inbound("/h"))   # 新别名
    assert res.ok
    flat = [b for row in res.payload["buttons"] for b in row]
    assert any(b.get("copy") == "/compact" for b in flat)
    assert all("send" not in b for b in flat)  # help 全部 copy 型:点击≠执行
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

- 抽一个模块级 helper(命令文件内,非 ABC):

```python
def _option_buttons(command: str, options: tuple[str, ...], per_row: int = 3) -> list[list[dict]]:
    btns = [{"text": o, "send": f"{command} {o}"} for o in options]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]
```

- `_cmd_model` / `_cmd_search_provider` / `_cmd_effort` 的无参分支:返回 `CommandResult(ok=True, message=…, payload={"buttons": _option_buttons("/model", self.available_models)})`。
- `_cmd_help` 无参分支:每个 CommandSpec 一个 copy 按钮 `{"text": spec.name, "copy": spec.name}`(2 列/行);`/h` 加进 `_handlers`(指向 `_cmd_help`)与 `_specs`(description "同 /help")。
- `/clear` `/streaming` 等不需要按钮——Task 1 已让它们各自成新卡,无需改 handler。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `feat(commands): /model /search_provider /effort 选项按钮;/help+/h copy 按钮`

---

### Task 4: compact 取消按钮(停流 + 不写回)

**Files:**
- Modify: `src/xana/agent_adapter/commands.py`(cancel 注册表 + 隐藏命令 + 流中按钮)
- Test: `tests/test_command_compact.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_compact_cancel_stops_stream_and_skips_writeback():
    # FakeModelClient 多 delta 脚本;在 progress_sink stub 收到第一个事件后,
    # 并发调 router.route_command(_inbound(f"/compact_cancel {run_id}"))
    # (run_id 从 progress 事件取)。断言:
    #   1) /compact 返回 ok=False 且 message 含 "已取消"
    #   2) store 里历史未被改写(原消息仍在,无 boundary)
    #   3) 后续不再有 progress 事件(流停了)
    ...


@pytest.mark.asyncio
async def test_compact_streaming_card_has_cancel_button():
    # progress 事件 payload["buttons"] == [[{"text":"取消","send":f"/compact_cancel {run_id}"}]]
    ...
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

- router 加 `self._compact_cancels: dict[str, asyncio.Event] = {}`。
- `_cmd_compact`:开始时注册 event;`_emit` 的 payload 加 `"buttons": [[{"text": "取消", "send": f"/compact_cancel {run_id}"}]]`(adapters 的 note_progress 路径要把 payload buttons 写进 `ts.draft.buttons`/`ts.last_buttons` — Task 2 已铺,确认 note_progress 也走同逻辑,没有则补);`generate_summary` 的 `on_progress` 回调开头检查 `if cancel_event.is_set(): raise _CompactCancelled`;捕获后:不写回、final `CommandResult(ok=False, message="🛑 压缩已取消", payload={"run_id": run_id})`、清理注册表(finally)。
- `_CompactCancelled(Exception)` 模块级;`generate_summary` 不需要改——异常会从 `client.stream` 消费循环冒出,但注意 compact.py:113 `except Exception: continue` 会吞掉它并重试!**必须**让 `_CompactCancelled` 穿透:`generate_summary` 的 `except Exception` 改为 `except _CompactCancelled: raise` + `except Exception: continue`(compact.py 是实现文件,可改;把异常类放 compact.py 导出,commands.py 引用)。
- `_cmd_compact_cancel`:`/compact_cancel <run_id>` → set event,`CommandResult(ok=True, message="", payload={"silent": True})`;隐藏命令:加 `_handlers` 不加 `_specs`(/help 不显示)。gateway/adapter 对 `payload["silent"]` 的 final 不发新卡(Task 1 的 `_notify_command_card` 跳过 `/compact_cancel` 前缀;实现者统一处理:gateway 对 silent 结果直接 return 不渲染)。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `feat(compact): 卡片取消按钮 — 停流、不写回、隐藏命令 /compact_cancel`

---

### Task 5: TG 渲染修复(单折叠工具块 + 禁链接预览)

**Files:**
- Modify: `src/xana/agent_adapter/channels/telegram_html.py:38-48`
- Modify: `src/xana/agent_adapter/channels/telegram_client.py`(send_message / edit_message_text)
- Test: `tests/test_telegram_render_turn.py`(追加/修改)

- [ ] **Step 1: 写失败测试**

```python
def test_tool_block_single_blockquote_no_pre():
    # render_tool_block 输出: <b>头</b>\n<blockquote expandable>args文本\n────────\n结果</blockquote>
    # 断言 "<pre>" not in html;html.count("<blockquote") == 1
    ...


@pytest.mark.asyncio
async def test_client_disables_link_preview_on_send_and_edit():
    # stub httpx 捕获 body;send_message 与 edit_message_text 的 body 均含
    # {"link_preview_options": {"is_disabled": True}}
    ...
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

- `render_tool_block` 改 yuxu `_render_one_tool_block_html`(yuxu telegram.py:652-707)形:body = `escape(args_json)` 与 `md_to_html(result)`?——**不**,yuxu 对 result 也是纯 `_html_escape`(避免 result 里的 markdown 再生成 `<pre>`/链接标签);照抄:`body_segments = [escape(args_json_compact), escape(result_clipped)]`,`"\n────────\n".join`,单 `<blockquote expandable>`,running 时只有 args + "⏳ 执行中…"。保留 clip 预算。
- `telegram_client.py`:`send_message` / `edit_message_text` 的 body 一律加 `"link_preview_options": {"is_disabled": True}"`(yuxu telegram.py:165-166,237-238 同款)。
- 受影响旧测试(断言 `<pre>` 或 md 渲染的)同步更新。

- [ ] **Step 4: 测试 + 回归**
- [ ] **Step 5: Commit** → `fix(telegram): 工具卡单折叠(去 pre 嵌套,yuxu 同构)+ 全局禁用链接预览`

---

### Task 6: 部署验证

- [ ] `python -m pytest -q`(已知 e2e 失败除外)→ `~/.xana/bin/xana sync` → tg/feishu 抽查:
  - 发 `/model` → 新卡 + 模型按钮,点按钮 = 切模型(又一张新卡确认)
  - 发 `/h` → 命令列表 + copy 按钮(tg 点击复制到输入区/剪贴板)
  - 发 `/compact` → 流式卡带"取消",点取消 → 卡片变"已取消",上下文未动
  - 让模型跑一个带链接输出的工具 → tg 单折叠、无链接预览卡

---

## Self-Review 结论

- 需求覆盖:新卡片抽象(T1,复用 compact 预建通道)✓;按钮抽象 send/copy(T2,模型未来可经既有 extract_buttons 用同能力)✓;/model /search_provider(/effort 顺带)按钮(T3)✓;/help+/h copy 按钮(T3)✓;compact 取消停流不写回(T4)✓;clear/streaming 等自动新卡(T1 对所有命令生效)✓;TG 双折叠修复 + 链接预览(T5,yuxu 同构,行号已锚定)✓。
- 锁定文件:零改动(payload/metadata 均自由 dict)。
- 两处显式调研步骤:feishu copy 行为支持(T2,给了回退方案);adapter final 按 run_id 找 turn 的落点(T1,要求先读 render 现状)。
