---
title: harness prompt assembly
type: doc
status: active
---

# harness prompt assembly

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [system_prompt.py](../src/xana/harness_sdk/system_prompt.py) — system prompt 静态常量 + `build_system_prompt()`
- [compact_prompt.py](../src/xana/harness_sdk/compact_prompt.py) — compact prompt 常量 + `get_compact_prompt()`
- [default.py](../src/xana/harness_sdk/default.py) — 每轮组装调用点（`_run_query_loop`，约 :922）与动态段收集（约 :650-693）

## 职责

把 request、context、技能、记忆注入、extension 贡献拼成一条 system prompt 文本 + 每轮 tools_schema。system prompt 与 tools_schema 在每轮 turn 循环内组装：deferred 工具被揭示后，下一轮即生效。

## system prompt 结构

### 7 个静态段（system_prompt.py 模块级常量）

1. `INTRO` — 身份与安全/URL 红线
2. `SYSTEM` — 输出即沟通、权限模式、prompt injection 警觉等
3. `DOING_TASKS` — 执行任务总则
4. `ACTIONS` — 谨慎执行操作（风险操作清单）
5. `USING_TOOLS` — 专用工具优先、todo_write、并行调用
6. `TONE` — 语气与风格
7. `OUTPUT` — 输出效率

### 13 段拼装（`build_system_prompt()`，模板 PROMPT_ASSEMBLY_ORDER.md）

位置固定为 13 个：INTRO、(identity)、SYSTEM、DOING_TASKS、ACTIONS、env、tools、deferred、dynamic_middle×2、USING_TOOLS、TONE、OUTPUT。规则：

- `override`（`config_snapshot["agent_system_prompt"]`）非空 → 跳过全部静态段，只保留动态段（env / tools / deferred / dynamic_middle）。
- identity（`config_snapshot["agent_identity"]`）插在 INTRO 与 SYSTEM 之间，仅 override 为空时生效。
- env 段（`env_section()`）：cwd、是否 git 仓库、平台、shell、OS、模型名。
- tools 段（`tools_section()`）恒为空字符串——完整 schema 走 `ModelRequest.tools_schema`（CC 同款），函数保留以固定 13 段拼装位。
- deferred 段（`deferred_tools_section()`）：未揭示的 defer 工具只列 (名字, 描述)，提示用 tool_search 加载。

### 动态段来源（`_run_query_loop` 每轮收集，按序拼入 dynamic_middle）

1. memory injector（`injected_head` / `injected_tail`，按 `placement` 分）
2. skill registry 激活产出的 prompt blocks
3. extension `contribute_turn_input` 的 tail fragments
4. L3 todo 块（阶段一落地）：`context.metadata["todo_store"]` 存在时每轮 `TodoStore.load(session_id)`，非空则渲染为 `<todo_status>` 块——每行 `序号. 状态符号 内容`（⏳ in_progress / ✅ completed / ☐ 其他），恒排在 base_parts 尾部。前缀缓存纪律：静态段 → env → L1/L2 注入 → L3 易变尾部（default.py:694-718）。
5. extension `PromptAssembler`（`ext.context`）——把以上 base_parts 与 extension 上下文拼成最终 dynamic_middle_text；无 extension 时就是 base_parts 的 join

### 事实来源

静态段最初逐字译自 CC 2.1.88（`constants/prompts.ts` 底版）；2026-09 对照 CC 2.1.276 做过一次克制微调（删除 CC 已删条目、补 CC 新增条目）。env 段模板逐字来自 AGENT_LOOP_PROMPT.md:123-130，deferred 段来自 :166-170。

## compact prompt（compact_prompt.py）

`get_compact_prompt()` = `NO_TOOLS_PREAMBLE` + `BASE_COMPACT_PROMPT` + `NO_TOOLS_TRAILER` 三段拼接：

- 头/尾两次强调"仅回复纯文本、禁止工具调用"（CC 同款：工具调用浪费唯一回合）。
- 正文要求先 `<analysis>` 后 `<summary>`，摘要固定 9 个小节（见 [harness_compact](harness_compact.md)）；含安全约束逐字保留与"仅 user 角色回合算用户消息"两段（2026-09 对照 CC 2.1.276 补入）。
- `COMPACT_BOUNDARY_MARKER = "对话已压缩"` 也定义在本模块。
- 本模块只管"让模型写摘要"的 prompt；"把摘要包给后续 prompt 看"的模板（`CompactSummary.POST_COMPACT_TEMPLATE`）单一来源在 `context/message.py`（§1.9 single-source rule）。

## 不变量

- system prompt 不区分注入来源（系统 / extension 直接拼接），但收集顺序固定：memory head → skills → memory tail → extension tail → extension assembler。
- 项目级规则文件（CLAUDE.md 类）目前不进 system prompt；CC 是包 `<system-reminder>` 作为 messages[0] prepend，xana 预留了同样位置（default.py 中 TODO(claude_md)）。
