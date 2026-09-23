---
title: context
type: doc
status: active
---

# context

## 定位

`context` 是 active context 的运行时载体：`ContextSDK` facade 以 per-(sid, agent) 的有序 `Message` 缓冲承载一个 session 当前可见的上下文，并提供 compact-as-placeholder 的并发协议。它是 [memory](memory.md) 模块 `ActiveContextStore` 合约背后的实际读写通道，不是独立的持久化权威。

## 代码锚点

- [sdk.py](../src/xana/context/sdk.py) — `ContextSDK` facade，context 状态的唯一合法入口
- [_store.py](../src/xana/context/_store.py) — 私有 `_ContextStore`：per-(sid, agent) buffer + per-sid 同步原语（import guard，只允许 sdk.py 导入）
- [message.py](../src/xana/context/message.py) — `Message` ABC 及子类（`UserMessage` / `AssistantMessage` / `ToolResultMessage` / `RegularPlaceholder` / `CompactPlaceholder` / `CompactSummary`）
- 组装点：[boot.py](../src/xana/runtime/boot.py) — `ContextSDK()` 在 boot 中构造，并回填给 `InMemoryActiveContextStore` 做写穿透（legacy 写同时进两个 store，读以 SDK 为准）

## 关键机制

- **per-sid 锁**：`_SidState.write_lock` 串行化所有 append/replace；`compact_lock` 保证同一 sid 同时最多一个 compact 在飞；`compact_event` + `compact_holder` 供 fork 等待方阻塞/唤醒。
- **append**：`append_user` / `append_regular_placeholder` 在 write_lock 下追加；`append_compact_placeholder` 额外取 compact_lock，把 PH 插到最早 `RegularPlaceholder` 之前，并 clear compact_event 使后续 fork 阻塞。
- **replace / cancel**：`replace_placeholder(sid, agent, run_id, replacements)` 按 run_id 定位 PH 原位替换；当 compact PH 被单个 `CompactSummary` 替换时，把 `[0:idx+1]` 整段折叠成摘要（模型永远看不到摘要前的旧内容），随后释放 compact_lock 并 set event。`cancel_compact_placeholder` 等价于空替换。
- **snapshot_for_fork**：若存在未替换且位于最早 RegularPlaceholder 之前的 CompactPlaceholder，则阻塞在 compact_event 上直到替换完成（循环重查，防 event 复用竞态）。
- **序列化**：`to_anthropic` / `to_openai` 接收 `system_content`，返回 system 消息 + buffer 的逐条序列化；空列表返回表示该 Message 对模型不可见（CompactPlaceholder 即如此）。
- **CompactSummary**：`render()` 是 model 侧与 UI 卡片侧共用的单一渲染源（`POST_COMPACT_TEMPLATE`），保证两边文案一致。

## Owns

- per-(sid, agent) 运行时 Message 缓冲及其并发协议。
- placeholder 的插入、替换、折叠规则。
- 面向 Anthropic / OpenAI 的序列化输出。

## Does Not Own

- 持久化（history mirror / markdown / SQLite 归 memory）。
- token 估算、compact 策略决策（策略在 harness/commands 侧，SDK 只执行占位协议）。
- 检索与注入。

## Inputs

- append / replace / cancel / snapshot 请求：`sid`、`agent`、`run_id`、`Message` 序列。

## Outputs

- `snapshot_for_fork` 返回的 Message 快照。
- `to_anthropic` / `to_openai` 的消息数组。
- 经 `InMemoryActiveContextStore` 写穿透后的 legacy `ActiveContext` 视图。
