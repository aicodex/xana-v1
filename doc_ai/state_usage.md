---
title: state usage
type: doc
status: active
---

# state usage

## 上级模块

- [state](state.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/state/contracts.py) — `UsageSnapshot`
- [interfaces.py](../src/xana/state/interfaces.py) — `UsageEstimator`（仅接口合约，无实现）、`RunStore.update_usage`
- [budget.py](../src/xana/state/budget.py) — `BudgetTracker`（跨 run 预算护栏，concrete helper，不抽 ABC）

## 职责

`UsageSnapshot` 是 token/成本计量的 frozen dataclass：`estimated_input_tokens` / `estimated_output_tokens`（估算值，默认 0）、`exact_input_tokens` / `exact_output_tokens` / `exact_cost_usd`（LLM 返回后的精确值，可空）、`model` / `provider`（计量归属）。

## 语义

- **双轨字段是有意的**：调用方先用估算值占位（turn 进行中），LLM usage 回调到位后经 `RunStore.update_usage(run_id, snapshot)` 整字段替换为带精确值的快照。`RunRecord.usage` 默认空快照，消费方必须容忍 `exact_*` 为 `None`。
- `RunStore.update_usage` 与 `checkpoint` 一样是全字段替换语义（读-改-写落盘，见 [state_run](state_run.md)）。

## UsageEstimator

`UsageEstimator.estimate(messages, model) -> UsageSnapshot`：**仅接口合约，当前无实现**。运行中实际的 token 估算与精确回填由 harness 侧承担（见 [harness_usage](harness_usage.md)），不经过本模块的 ABC。`UsageSnapshot` 数据类型是 state 合约的一部分，estimator 只是预留的估算边界。

## BudgetTracker（budget.py）

跨 run 预算护栏，boot 组装的 concrete helper（不抽 ABC），经 harness context metadata 注入：

- **累计**：`record(session_id, usage)` 在 harness 每次 `update_usage` 写点调用，把一次 run 的用量计入 per-session / per-day 累计（token 取 exact 优先、缺失退回 estimated，语义同 UsageAccumulator）。
- **检查**：`check(session_id, budget, usage)` 返回类型化超限原因字符串（`run_budget_exceeded` / `session_budget_exceeded` / `day_budget_exceeded`），未超限返回 None；调用侧不累计（累计只在 record）。
- **budget 字典键**（全部可选）：`max_tokens`（单 run 上限，每轮检查）、`max_session_tokens`（单 session 累计）、`max_day_tokens`（当日累计）；构造参数 `session_limit` / `day_limit` 是 budget 缺省时的全局兜底。
- harness 把超限原因翻译成收尾信号（progress 事件 + `RunRecord.metadata["budget_exceeded"]`），触发优雅落地而非裸死，详见 [harness_usage](harness_usage.md)。

## 不变量

- usage 数据随 RunRecord 落盘（有 `history_root` 时经 `FileRunStore`），不存在独立的 usage 存储。
- budget 上限字典是 `RunRecord.budget` 合约字段（随 run 落盘）；per-session/per-day 累计状态只在 BudgetTracker 内存中，不落盘、不跨进程。
- `exact_*` 可空——估算先行、精确后补，消费方不得假设精确值存在。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
