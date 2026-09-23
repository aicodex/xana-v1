---
title: agent adapter rendering
type: doc
status: active
---

# agent adapter rendering

## 上级模块

- [agent_adapter](agent_adapter.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agent_adapter/contracts.py) — `DraftHandle` / `RenderedMessage`
- [interfaces.py](../src/xana/agent_adapter/interfaces.py) — `DraftRenderer` ABC
- [unified_card.py](../src/xana/agent_adapter/channels/unified_card.py) — `UnifiedCardState` 平台无关卡片模型
- [unified_card_events.py](../src/xana/agent_adapter/channels/unified_card_events.py) — `apply_event` 状态机
- [card.py](../src/xana/agent_adapter/channels/card.py) — `Card` ViewModel
- [unified_card_feishu.py](../src/xana/agent_adapter/channels/unified_card_feishu.py) / [unified_card_telegram.py](../src/xana/agent_adapter/channels/unified_card_telegram.py) / [unified_card_cli.py](../src/xana/agent_adapter/channels/unified_card_cli.py) — 平台渲染器
- [progress.py](../src/xana/agent_adapter/progress.py) — `AdapterProgressSink`

## 职责

把 agent/harness 事件流变成平台特定的可视消息：draft 的打开/更新/关闭，以及协议特定的渲染输出。

## DraftRenderer

仅接口合约，无实现：v1 没有 `DraftRenderer` 实现类。`DraftHandle`（adapter_id/session_id/draft_id）与 `RenderedMessage`（text + 可选 draft + metadata）仍是 gateway 与 adapter 之间的渲染契约——gateway 在 `submit` 中把 adapter.render 的结果以 `RenderedMessage` yield 给消费者，并用 `metadata.event_kind` 标注事件类型。

## v1 实际渲染栈：UnifiedCard（MVVM）

- Model：`UnifiedCardState`，平台无关卡片数据（compact 段、answer 段含 thinking/tools、menu 段、footer 三态 fg_running/bg_frozen/terminal、top/bottom 按钮、expand 分页模式）。
- 事件桥：`apply_event(state, event)` 消费 HarnessEvent / AgentEvent / ProgressEvent，按状态机原地迁移 state；纯函数，无 I/O。
- ViewModel：`Card`——adapter 不再关心何时刷新/冻结/合并。`card.dispatch(event)` 自动 render+deliver，`freeze()`（detach 瞬间）、`terminalize()`（ph_replaced）、节流（`DraftCoalescer` 0.25s）与 drop/dirty 判断都在 Card 内；平台交付与 task 追踪经 `bind_transport` 注入。
- View：三个平台渲染器——飞书输出 interactive card schema dict（collapsible_panel，工具 body 限 800 字防 30KB 上限，markdown 图片降级为纯文本防 image_key 400）；TG 输出 MarkdownV2 文本 + inline_keyboard（4096 上限的分段预算 + thinking/tools/answer 展开分页）；CLI 输出纯文本（oneshot 终态视图，xanac 用）。
- 每个 PH / 菜单卡对应一个 `Card`，keyed by card_id；detach 的卡冻结（`_THAW_KINDS` = ph_replaced / run_completed_bg 解冻），compact 进度类事件（compact_started/progress/finalized）即使 detached 也照常渲染。

## 辅助机制

- `AdapterProgressSink`：harness `ProgressSink` → adapter 的 `note_progress` 分发，只转发 `usage_progress` 和 `compact_progress` 两种 payload，并补写 `run_id` 便于 adapter 按 run 路由。
- `PairingStore`（`_pairing.py`）：per-platform 用户 allowlist + pending 队列，JSON sidecar（`~/.xana/channels/pairings.json`），mtime 热重载使长跑的 `xana serve` 能看到另一个进程里 `xana pair` 的审批。
- `TurnDraft`（`_draft.py`）：旧 per-turn 渲染状态，仍被部分路径引用；新卡路径以 UnifiedCardState 为准。
