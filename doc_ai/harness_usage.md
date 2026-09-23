---
title: harness usage
type: doc
status: active
---

# harness usage

## 上级模块

- [harness_sdk](harness_sdk.md) — 模块设计

## 代码锚点

- [usage.py](../src/xana/harness_sdk/usage.py) — `DefaultUsageAccumulator`
- [contracts.py](../src/xana/state/contracts.py) — `UsageSnapshot`（定义在 state 层，harness 只消费）
- [budget.py](../src/xana/state/budget.py) — `BudgetTracker`（消费方在 harness）
- [default.py](../src/xana/harness_sdk/default.py) — 累积与上报调用点（usage 事件处理约 :980；预算检查约 :1036）

## 职责

把 provider 上报的 usage 事件合并成 run 级 `UsageSnapshot`，挂在 `HarnessEvent(kind="final")` 上，并写回 `RunRecord`。

## 类型

`UsageSnapshot`（state/contracts.py:34）：`estimated_input_tokens` / `estimated_output_tokens`（int 默认 0）、`exact_input_tokens` / `exact_output_tokens` / `exact_cost_usd`（可选）、`model` / `provider`。

注意：旧文档写的 `TurnUsage` **在代码中不存在**。

## 累积规则（`DefaultUsageAccumulator.apply_model_event`，镜像 CC updateUsage）

- 只处理 `event.kind == "usage"` 且 `event.usage` 非空的事件；其余原样返回。
- **exact_input_tokens = input + cache_read + cache_write**：cache 命中/写入也占上下文，provider 把 cached 部分从 input_tokens 拆走了，必须并回。三者全空或合计为 0 → 沿用旧值（None/0 语义是"保持不变"）。
- **exact_output_tokens**：非空覆盖，为空沿用。
- 不做本地 tokenizer，provider usage 事件即事实源；provider 完全不上报时快照停在预估值。
- estimated 字段设计上只在 `estimate_before_request()` 设一次且不被 exact 覆盖（cc 同款规则）；启发式 `_CHARS_PER_TOKEN = 4`，与 compact 的 `estimate_tokens()` 同源。

## 与 auto-compact 的耦合

- `exact_input_tokens` 首次到达时，以 `(prompt_tokens, history_len)` 存入 `_session_prompt_tokens[(session_id, agent_name)]`，auto-compact 用它做精确 token 判断（见 [harness_compact](harness_compact.md)）；历史变短（/clear 等）时旧值失效，退回字符估算。

## 进度上报

每个 usage 事件经 `ProgressSink` 发 `usage_progress`（model、tokens_in/out、elapsed_s、context_window）；turn 开头还会抢发一次零值事件，让 adapter footer 立刻拿到 model 与 context_window，不留空窗。

## 预算护栏（§4.3）

run 的 budget 上限经 `HarnessRequest.metadata["budget"]` 进入（缺省/非 dict 视为无上限），`BudgetTracker` 实例经 `HarnessContext.metadata["budget_tracker"]` 注入（boot 组装，见 [runtime_boot](runtime_boot.md)）：

- **每轮检查**：turn 内每个 usage 事件累积后，若 run 有 budget 且尚未超限，调 `budget_tracker.check(...)`；超限记 `budget_exceeded` 原因，并经 progress_sink 发 `budget_exceeded` ProgressEvent（kind/reason/tokens_in/out）。
- **优雅落地**：标记后当前 assistant 消息照常落库，随后 break 出 tool loop——不再开新轮、不派发工具，带已产出文本走正常收尾（有 final 事件，非裸死）。
- **收尾写点**：`RunRecord.metadata["finished_at"]` 照常写；超限原因写入 `RunRecord.metadata["budget_exceeded"]`（阶段四 episode 状态机的消费方）。
- **跨 run 累计**：收尾时 `budget_tracker.record(session_id, usage)` 把本次用量计入 per-session / per-day。

BudgetTracker 的语义（键、类型化原因、兜底 limit）见 [state_usage](state_usage.md)。

## 现状备注

- `DefaultHarness` 目前**没有调用** `estimate_before_request()`——快照直接以 `UsageSnapshot(model=…, provider=…)` 初始化（default.py:738），estimated 字段恒为 0；预估值只在无 provider 上报时作为回退语义存在。
