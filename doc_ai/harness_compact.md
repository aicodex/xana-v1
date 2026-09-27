---
title: harness compact
type: doc
status: active
---

# harness compact

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [compact.py](../src/xana/harness_sdk/compact.py) — 共享纯函数（auto-compact 与手动 /compact 共用）
- [compact_prompt.py](../src/xana/harness_sdk/compact_prompt.py) — compact prompt 与 `COMPACT_BOUNDARY_MARKER`
- [default.py](../src/xana/harness_sdk/default.py) — auto-compact 同步触发点（`_run_query_loop` 内，约 :773-893）
- [context/message.py](../src/xana/context/message.py) — `CompactSummary`（:141-179）

## 职责

上下文超限时的摘要式压缩。本模块无 class、无 ABC；状态写回走已有的 `ActiveContextStore.set()` / SDK replace_placeholder。boundary / summary / 保留消息都是普通 `MemoryMessage`，ActiveContext 的 summary 字段不再使用。

## auto-compact 同步触发（DefaultHarness 内联）

每轮 turn 循环开头检查（token 数 vs config 线）：

- token 来源：优先用 usage 事件存下的精确 prompt tokens（`_session_prompt_tokens`，见 [harness_usage](harness_usage.md)），历史变短（/clear）时退回 `estimate_tokens()` 字符估算（`_CHARS_PER_TOKEN = 4`）。
- 触发线：`tokens >= config["force_compact"]` 且本轮尚未尝试过（`compact_attempted`，每 turn 只压一次）→ 同步执行压缩，期间发 `compact_progress` progress 事件（start / failed / done）。
- 保底线：`tokens >= context_window - BLOCKING_HEADROOM`（`BLOCKING_HEADROOM = 3000`）且已尝试过压缩 → turn 报错退出（"上下文已满且压缩未能降低占用，请 /clear 或更换模型"）。
- 压缩窗口 = 最近一条 compact boundary 之后的消息（`messages_since_boundary()`）。

## 摘要生成（`generate_summary`）

- 同模型、无 tools、单回合；prompt 见 [harness_prompt_assembly](harness_prompt_assembly.md)（compact prompt）；`thinking: disabled`（CC 同款），reasoning 通道 delta 不进摘要缓冲。
- 摘要请求是纯文本转录：tool 消息降级为 `[工具 X 的结果]` 的 user 文本，assistant 的 tool_calls metadata 丢弃（否则 Anthropic 方言拼出孤儿 tool_result 400）。
- `<summary>` 缺失或传输异常都重试，`_COMPACT_RETRIES = 3` 次后返回 None（报 failed）。
- 提取锚定 `</analysis>` 后的第一个 `<summary>`（首尾标签都可能被正文复述，不可靠），闭合用 `rfind("</summary>")`；`partial_summary` 公开别名供 /compact 流式进度取部分摘要。
- `summary_progress()` 把流式文本映射成 9 格进度条 + 当前小节状态（见到第 n 节标题即前 n-1 节完成）。

## 9 节摘要（`SUMMARY_SECTIONS`）

主要请求与意图 / 关键技术概念 / 文件与代码部分 / 错误与修复 / 问题解决 / 所有用户消息 / 待处理任务 / 当前工作 / 可选的下一步。逐字对应 compact prompt 要求的 `<summary>` 结构。

## 写回路径（Bug C fix：优先 SDK Compact-as-PH）

1. **transcript 诚信落盘（compact 前）**：摘要生成成功后、写回前，`_persist_compact_transcript()`（default.py:1195-1216）把将被压缩的消息区间逐条写成 `<sessions 根>/{sid}/compact-{微秒时间戳}.jsonl`（sessions 根 = `metadata["runs_dir"]` 的父目录，缺省当前项目的 `~/.xana/projects/<id>/sessions`）（每行 `{role, content, metadata}` JSON；`XANA_HOME` 可改根目录）。落盘成功 → `CompactSummary` 的 transcript 路径指向这个真实可读文件；落盘失败 → 退回旧式相对路径 `runs/autocompact-{session}-{run}.jsonl`，不阻断 compact。修复了此前 default.py 虚构 transcript 路径、文件并不存在的问题。
2. **SDK Compact-as-PH（优先）**：经 `ContextSDK.append_compact_placeholder` + `replace_placeholder` 写入，run_id 形如 `autocompact-{session}-{run}`，transcript 为上一步的真实落盘路径。收益：与手动 /compact 走同一把 per-sid compact_lock（auto 与 manual 串行化），boundary marker 留在 SDK buffer（legacy `set()` 镜像经 `_message_bridge` 丢 role="system"，boundary 会丢）。
3. **legacy fallback**（store 无 SDK 时）：有 write_queue → `ReplaceRangeItem` 替换 `[0, fork_point)`（fork_point = 第一个 running PH 的下标），boundary metadata 记 `compact_boundary: True` + `compact_summary: True` + `boundary_marker`；无 write_queue → `store.set(build_post_compact_context(...))`。

写回后清 `_session_prompt_tokens` 缓存并重读快照继续本轮。

## revealed tools 保留

压缩会清空上下文，deferred 工具的揭示状态不能丢（compact.py:143-152）：`revealed_tools_from_messages()` 汇总历史中所有消息的 `revealed_tools` ∪ compact boundary 的 `pre_compact_revealed_tools`（CC preCompactDiscoveredTools 同思路），写进新 boundary metadata 的 `pre_compact_revealed_tools`，压缩后下轮 `_split_tool_schemas` 据此恢复。

## CompactSummary（context/message.py:141-179）

post-compact 消息的单一来源渲染器：`POST_COMPACT_TEMPLATE`（"此会话正在从上一次因上下文不足而中断的对话中继续…" + 摘要 + transcript 指引 + "不要确认摘要、直接继续"），`render(summary, transcript_path)` 同一条字符串同时供模型 prompt（`to_anthropic`/`to_openai` 渲染为 user 消息）与 UI 卡片正文（compact_finalized 事件），保证两侧一致。SDK 路径直接物化为 `CompactSummary` 消息节点；legacy 路径用 `CompactSummary.render()` 包成 user `MemoryMessage`。

## post-compact 上下文形状

CC 行为，不逐字保留近期消息：`build_post_compact_context()` 返回 boundary（`role="system"`，内容 = `COMPACT_BOUNDARY_MARKER` "对话已压缩"）+ 一条 post-compact 消息（`CompactSummary.render()`）。
