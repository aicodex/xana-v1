---
title: Codex 插话机制源码审阅
type: experience
status: reference
date: 2026-11-08
source_commit: 748ab55ca7de0b647d9a7a9d9a8e70e00f28666d
---

# Codex 插话机制源码审阅

## 背景与结论

Xana 正在评估删除同一会话多 run、普通占位符和完成后回填机制，同时希望用户可以及时补充要求，保留已经完成的工作。本次审阅以 OpenAI Codex 的公开 main 源码为依据，区分实际实现与适用于 Xana 的建议。

源码版本：[748ab55ca7](https://github.com/openai/codex/commit/748ab55ca7de0b647d9a7a9d9a8e70e00f28666d)，提交时间 2026-11-08 05:59:11 UTC。

主要启发：维持一个活动 turn，把新输入加入它的队列；分别控制当前模型请求、等待结果的调用和实际执行任务。插话可以使模型改用新输入继续处理，而已完成的历史与可继续运行的任务保留。

## 当前源码的两种路径

`instant_interrupt` 在该 main 版本中默认开启，状态仍为 `UnderDevelopment`，不能据此断言所有已发布客户端和端点的行为相同。此前本地 2026-09-18 的克隆未包含这套快速插话路径，不能用旧实现替代本次结论。

- 关闭时：新输入进入当前 turn 的 pending 队列，在本次模型请求和工具处理结束后消费，继续原 turn。
- 开启时：每次模型请求有独立的 `preempt` 信号。收到用户输入后可以提前结束当前采样或让 code-mode 等待返回，然后在同一个 turn 中继续。

依据：`codex-rs/features/src/lib.rs` 的 `InstantInterrupt` 定义与默认值；`core/src/session/mod.rs` 创建 `StepContext.preempt`；`core/src/session/input_queue.rs::watch_user_input`；`core/src/session/turn.rs::run_sampling_request`。

## 输入和执行的边界

| 场景 | 源码行为 | 保留的工作与限制 |
|---|---|---|
| 当前有常规 turn | `turn/steer` 校验 `expectedTurnId`，把输入加入该 turn | 没有另起一个前台回答循环；接收成功不等于已被模型消费 |
| 模型正在生成 | 快速路径可提前结束采样；支持相应协议的 WebSocket 使用 `response.interrupt` 并排干响应 | 已完成条目进入后续上下文，未完成片段可能丢弃 |
| 普通工具正在执行 | 已分发的工具由单独的取消信号控制，采样结束后仍收集工具结果 | 插话不会普遍终止普通工具，也不能保证立即跳过一个长时间阻塞的工具 |
| code-mode exec 或 wait | 提前返回本次观察，实际 cell 继续执行，返回 cell ID | 后续可以继续 wait 获取结果；这是显式的工具执行协议 |
| sleep 或等待子 agent | 新输入唤醒等待并产生正常工具结果 | 唤醒等待本身无需终止被等待的子任务 |
| 自动压缩正在运行 | 输入保持 pending，压缩后继续处理 | 压缩与输入消费有专门顺序控制，不能把新消息随意混入压缩请求 |
| turn 即将结束 | 收尾阶段检查并处理晚到输入；界面处理活动 turn 变化的竞态 | 避免接收成功后直接漏过补充消息 |
| 用户明确 interrupt | 走独立的 turn 取消路径 | 取消不等于撤销已经发生的文件写入或外部操作 |

官方接口说明：[Codex App Server](https://developers.openai.com/codex/app-server)。输入准入在 `core/src/session/turn_input.rs::steer_input`，接口入口在 `app-server/src/request_processors/turn_processor.rs::turn_steer_inner`。

## 已完成工作如何保留

`StepContext.preempt` 与 turn 的 `cancellation_token` 分离。前者用于插话，后者用于真正的取消。`turn.rs` 在结束本次采样后通过 `drain_in_flight` 收集已分发工具的结果；新输入随后加入历史，再发起下一次模型请求。

code-mode 的正式契约更明确：`execute` 和 `wait` 在收到 preempt 时让本次观察返回，cell 继续运行。gRPC 实现通过 `yield_observation` 请求交还控制，不等同于 terminate。对长任务而言，这比仅把全部工具调用标成可取消更关键。

支持相应上游协议时，WebSocket 请求 `response.interrupt`，使用 `discard_partial_items` 模式，并继续接收终止边界与完整条目；测试检查了后续请求继续使用该响应 ID，以及中断响应的用量记录。未完成的推理或输出可能被丢弃，不能承诺所有已消耗 token 都能复用。其他传输路径也不能直接获得同样的协议保证。

接收、写入历史、持久化和模型消费是不同阶段。Core 的提交返回值只确认准入决定；界面另外维护 pending steer，通过消息 ID 和历史回执确认消费，并在中断时处理未确认输入。Xana 的恢复设计仍需明确这些阶段，不能把接口返回成功解释成已完成持久化。

## 已检查的测试证据

以下测试位于 `codex-rs/core/tests/suite/pending_input.rs`：

- `steer_interrupts_and_drains_websocket`：处理中断接受、拒绝和缺少确认，检查连接继续使用及用量。
- `steers_during_tool_drain_preserve_tool_output_and_each_input`：连续补充两条消息，保留真实工具结果且只记录一次。
- `steers_yield_exec_and_wait_without_stopping_the_cell`：插话让 exec 和 wait 返回，cell 最终仍完成并交回结果。
- `input_preempts_response_and_yields_running_code_mode_call`：已经运行的 cell 保留，旧响应尚未交付的后续工具调用不再执行。
- `steer_during_compaction_is_sent_after_compaction`：压缩期间接收的输入在压缩后处理。
- `user_input_does_not_preempt_after_reasoning_item`：显式关闭快速开关，验证原来的等待路径。

`pending_input_persistence.rs::steer_during_final_flush_reaches_the_model_before_turn_completion` 还覆盖收尾持久化期间插话的竞态。

本次检查实现和测试断言，未执行 Rust 测试；源码测试表达的是预期契约，不构成当前机器端到端运行验证。

## 对 Xana 的建议与后续

建议评估“每会话一个活动执行循环 + 待处理输入 + 独立工具任务”的结构。删除前台对话专用的多 run 回填，不影响不同会话、定时任务和显式后台任务的并行能力。

实现时应先统一输入的准入、排序和确认语义，再决定模型请求能否提前结束。针对 Kimi，先以完整模型响应和工具结果边界接入补充消息；更快的采样中断需要单独验证流关闭、完整条目保留及用量，不能直接假设支持 OpenAI 的 WebSocket 协议。

视频等长任务适合快速返回任务 ID，通过查询或可提前结束的等待获取进度。用户补充时只交还对话控制，生成任务继续运行；已提交任务与产物信息须持久保存。真正停止生成应是明确的取消动作，并由工具报告取消结果。

## 后续实现参照约束

以下为 Xana 后续设计与验收的参照，尚未实施：

- **明确输入意图**：排队表示当前 turn 完成后处理；插话表示补充当前 turn；取消表示停止指定执行。三个动作应有明确入口，普通补充消息不能自动触发全链路取消。
- **每会话一个活动循环**：插话进入当前循环的输入队列；不同会话、子任务和定时任务可独立运行。后台工具完成后交回结果，由所属循环消费，避免在主历史中进行多个回答分支的乱序合并。
- **等待与执行分别控制**：让一次 wait 返回，不应终止被等待任务；任务 ID、运行状态和结果位置应可查询。长工具不支持交还控制时，先在完整结果边界处理插话，不能伪造完成结果。
- **已完成过程可继续使用**：保留有效的消息、工具调用及其结果、产物和任务 ID；模型请求提前结束时只使用已确认完整的条目，未完成的参数不得作为工具调用执行。
- **输入确认可追踪**：以消息 ID 标识接收、持久化、加入上下文和已提交模型的阶段；重试、连续插话、取消、收尾和重启应有明确恢复规则，防止漏处理或重复执行外部操作。
- **能力与迁移边界明确**：Kimi 的快速采样中断须独立验证；现有占位历史先备份并迁移，压缩提交与原始转录保留，不按类名批量删除。前台对话的简化不改变调度和显式后台任务的职责。

迁移验收应覆盖：推理中插话、普通工具中插话、长任务等待中插话、多条输入排序、收尾竞态、压缩期间输入、取消后未消费输入恢复，以及重启后查询原任务。具体删除范围和持久数据迁移需在实施前明确；本次仅记录源码经验。

基于 Xana 当前代码和持久数据的具体路径见[单循环对话与取消机制迁移方案](../docs/design/single-loop-conversation-migration.md)。
