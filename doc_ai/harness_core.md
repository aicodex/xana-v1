---
title: harness core
type: doc
status: active
---

# harness core

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [default.py](../src/xana/harness_sdk/default.py) — `DefaultHarness` 唯一实现
- [contracts.py](../src/xana/harness_sdk/contracts.py) — `Harness` / `HarnessEvent` / `HarnessRequest` / `HarnessContext` / `AbortSignal`
- [interfaces.py](../src/xana/harness_sdk/interfaces.py) — `Harness` ABC

## 职责

`DefaultHarness.invoke()` 是 turn 的入口：消费 `HarnessRequest` + `HarnessContext`，产出有序 `HarnessEvent` 异步流。内部两级：

- `_invoke()`（default.py:344）— **PH ritual 与模式分派**。有 `ActiveContextStore` 时：先快照真实 store（保留其他并发 run 的 Placeholder），再追加 user 消息和 running 状态的 `Placeholder`（UI 可见），然后用快照+user 消息 seed 一个 `_ForkedActiveContextStore`，本 run 只在 fork 上工作，完成后把 fork 产出的消息写回替换 PH。无 store 时退化为不走 PH 的旧同步路径。
- `_run_query_loop()`（default.py:592）— **同步 turn 驱动**，sync 路径内联驱动，async 路径放进后台 task；两者共享。逐轮：组装 system prompt + tools_schema（deferred 揭示状态从历史 metadata 恢复）→ `client.stream()` 缓冲 tool_calls（中途不分发）→ 追加 assistant `MemoryMessage`（含 tool_calls / reasoning_content metadata）→ 有 tool_calls 则逐个 `_dispatch_tool` 并追加 role="tool" 结果消息 → 无 tool_calls 则 break 产出 `final`。`_MAX_TURNS = 25` 防失控循环。

## sync 与 async 的唯一差异 = consumer（Spec §1.5）

- sync：`_invoke` 直接 inline 消费 loop，finally 里走 `_finalize_writeback`。
- async（`context.async_mode=True` 且 metadata 提供 `main_writer` + `run_jsonl_writer`）：fork + `asyncio.create_task` 后台跑同一 loop，事件经 queue 转发给 consumer；PH ritual 两种模式都执行，便于 `/attach` 看到在途 run。
- 写回统一走 `write_queue`（`ReplaceItem`），回退 `MainWriter.enqueue_replace_placeholder`，再回退直写 store。失败 run 若无产出，合成一条 "此回答未完成" 的 system 消息写入。

## Placeholder 机制

- `ph_expand.py` 的 `expand_placeholders()`：发给 LLM 前把 mixed snapshot 里**其他 run** 的 `Placeholder` 展开为合成 user/assistant 对（"此问题正在后台处理中…"）。本 run 永远看不到自己的 PH（fork 快照在追加 PH 之前截取）。
- compact 的 PH 化写回见 [harness_compact](harness_compact.md)（SDK Compact-as-PH 路径）。

## 私有辅助类

- `_ForkedActiveContextStore`（default.py:1173）— 实现 `ActiveContextStore`。seed 之后 role ∈ {assistant, tool, system} 的 append 记入 `_produced`，成为 PH 替换 payload；`set()`（compaction 用）把 post-compact 上下文整体转入 `_produced`；`snapshot()` 返回含 PH 的 mixed 列表供 ph_expand。
- `_RunJsonlMirror`（default.py:1278）— 实现 `HistoryMirror`，把每行 append 进 per-run JSONL（`runs/{run_id}.jsonl`）；只写不读，读权威仍是主 HistoryMirror。

## 事件流

`HarnessEventKind`：turn_started / model_delta / tool_started / tool_finished / paused / cancelled / final / error。注意：

- `paused` 在合约里有枚举值，但 `DefaultHarness` 从不发出；pause 是 state 层概念（如 `state/startup_scan.py` 把中断 run 标为 paused，恢复决策见 [state_resume](state_resume.md)）。
- 取消 = `AbortSignal.trigger(reason)` 翻 `aborted` 标志，loop 每轮开头与 model streaming 中轮询；命中后发 `cancelled` 事件并触发 extension `on_turn_abort`。执行中工具的协作式取消见 [harness_tool_dispatch](harness_tool_dispatch.md)。

## 不变量

- 状态权威不落在 harness：context 在 `ActiveContextStore`，durable log 在 `HistoryMirror`，run 元数据在 `state.RunStore`。
- extension 生命周期 hook（thread/turn/tool）经 `_safe_hook` 包裹：observer 类 hook 异常只记日志，数据产出类 contributor 保持 fail-fast。
