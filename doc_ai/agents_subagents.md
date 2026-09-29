---
title: agents subagents
type: doc
status: active
---

# agents subagents

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agents/contracts.py) — `SubagentRequest` / `SubagentResult` / `DelegationMode` / `DelegationPolicy`
- [interfaces.py](../src/xana/agents/interfaces.py) — `SubagentDispatcher` ABC
- [gateway.py](../src/xana/agent_adapter/gateway.py) — `GatewaySubagentDispatcher` / `GatewayAgentSpawner`（首个实现）
- [default.py](../src/xana/harness_sdk/default.py) — `_ForkedActiveContextStore`（harness 侧 fork 机制）
- [ph_expand.py](../src/xana/harness_sdk/ph_expand.py) — Placeholder → 合成消息对展开
- [agent_runner.py](../src/xana/agent_adapter/agent_runner.py) — `GatewayAgentRunner`（`AgentRunner` 实现）
- [agent_run.py](../src/xana/tools/builtin/agent_run.py) — `agent_run` 委派工具
- [delegation.py](../src/xana/agents/delegation.py) — 子 agent 工具上限 / 有效工具 / 可委派列表（`SUBAGENT_EXCLUDED`）
- [agent_listing.py](../src/xana/extension_api/builtin/agent_listing.py) — `AgentListingContributor`（委派引导 + 可委派列表 note）
- 设计：[agent-delegation-design](../docs/design/agent-delegation-design.md)

## 职责

定义 subagent 委派的请求/结果契约与委派策略，以及 `SubagentDispatcher` 分发接口。

## 契约

- `SubagentRequest` — `parent_session_id` / `parent_agent` / `parent_run_id` / `target_agent` / `task` / `new_session_id`，外加形态控制：`mode`（`inline / background / isolated`，默认 `isolated`）、`fork_context_messages`、`inherit_cwd`（默认 `True`）、`isolation`、`metadata`。
- `SubagentResult` — `parent_session_id` / `child_session_id` / `target_agent` / `ok` / `result` / `transcript_ref` / `metadata`。
- `DelegationPolicy` — `allow_subagents`（默认 `True`）、`max_depth`（默认 1）、`allowed_agents`、权限与工具继承方式（`inherit / override / intersect`，默认 `intersect`）、`context_fork`（`none / messages / full`，默认 `none`）、后台/prompt 相关开关。

## 当前状态

`SubagentDispatcher` 已有首个实现 `GatewaySubagentDispatcher`（agent_adapter/gateway.py，boot 构造注入，同时挂 `gateway.subagent_dispatcher` 和 `BootResult`）：

- **深度门禁**：父 session depth ≥ `DelegationPolicy.max_depth`（默认 1）时拒绝，返回类型化错误 `SubagentResult(ok=False, metadata={"error": "max_depth_exceeded", "max_depth", "depth"})`，不抛异常。
- **lineage 权威写点**：dispatcher 持有完整 `SubagentRequest`，通过时先落库子 `SessionRecord`（`parent_session_id` / `parent_agent` / `parent_run_id` / `depth=parent.depth+1`，cwd 继承父），再调 spawner。
- **派发**：调 extension_api `AgentSpawner.spawn(target_agent, task, new_session_id, parent_run_id, fork=fork_context_messages)`，成功返回 `SubagentResult(ok=True, transcript_ref=f"runs/{run_id}.jsonl", metadata={"run_id": run_id})`。

extension_api `AgentSpawner` 的首个实现 `GatewayAgentSpawner` 与 dispatcher 同文件：spawn 走 gateway `_submit` 同款链路（InboundMessage 带 `spawn_run_id` / `spawn_background` / `target_agent` metadata，fire-and-forget 后台排干），写/补 `SessionRecord.parent_*`；`fork=True` 时经 `ContextSDK.snapshot_for_fork` 把父上下文快照拷贝为子 session 初始前缀——父缓冲只读共享（子不写入父）、子 run 只 append 私有后缀、结果不做上下文物理合并（回流走后续阶段的账本/变更单，防分支污染）。fork 的父 session 从子 SessionRecord.parent_* 解析，无 lineage 记录时退回自身 session（退化自拷贝）。详见 [agent_adapter_gateway](agent_adapter_gateway.md)。

**同步运行子 agent：`AgentRunner`**（`extension_api/capabilities.py`，实现 `agent_adapter/agent_runner.py` 的 `GatewayAgentRunner`）。`AgentSpawner` 是发射后不管、只返回 run_id、且经 `_submit` 会触碰渠道；需要等结果的场景（`agent_run` 委派、deep_research 的并行子研究员）用 `AgentRunner.run(agent_name, prompt, session_id, parent_run_id, parent_session_id, tool_ceiling, on_tool) -> AgentRunOutcome(run_id, status, text, tool_counts, error)`：直接调已注册 agent 的 `send()` 收集 `final`，不发卡片、不切换前台；子会话不存在时先落 `SessionRecord`（`parent_session_id` / `parent_run_id` / `depth=1`），并在第一条任务前加 `SUBAGENT_PREAMBLE`（译自 Kimi Code：主 agent 只看得到最后一条消息、不要直接问最终用户；resume 不重复），同一 `session_id` 再次运行即在原子会话上下文里继续；`tool_ceiling` 经 `AgentRequest.tool_ceiling` 传给子 run（None = 不限）；`on_tool` 按工具名回调供调用方汇总进度；等待任务被取消时触发子 run 的 AbortSignal。子 run 的 metadata 带 `parent_run_id`（RunRecord 记父子关系、子 run 不注入无进度提醒）。deep_research 防递归靠子 agent 的工具白名单（researcher 不含 deep_research）。boot 只构造一个 `GatewayAgentRunner`，`agent_run` 与 deep_research 共用。

**`agent_run` 委派**（`tools/builtin/agent_run.py`，`agents` 组，`concurrency="parallel"`）：输入 `description` / `prompt` / `subagent_type`（默认 chat）/ `resume`（与 subagent_type 二选一）。

- **子 agent 工具** = 它自己的工具选择解析结果 ∩ 上限；上限 = 调用方本 run 可用工具（`ToolContext.available_tools`，None = 目录全部）− `SUBAGENT_EXCLUDED`（`agent_run` / `agent_define` / `EnterPlanMode` / `EnterWorktree` / `report_goal` / `schedule` 组）。子 agent 永远拿不到 `agent_run`，委派深度固定为 1，不做深度计数。harness 端同样取交集（见 [harness_tool_dispatch](harness_tool_dispatch.md)）。
- **子会话 id**：`{父会话}-agent-{agent 名}-{hex6}`，结果里作为 `agent_id` 返回；`resume=<agent_id>` 在同一子会话继续，只接受本会话派出的子会话。
- **并发**：进程内同时运行的委派上限 6，超出返回 `retryable=False` 错误（"不要重试,直接用自己的工具完成"）。
- **结果**：头行含 agent_id、状态、用时、工具次数，接子 agent 最终文本，末尾提示可 resume；子 agent 非 completed 时 `ok=False` 附原因。执行中经 `report_progress` 上报子 agent 工具调用次数；父 run 取消时经 AbortSignal 中止子 run。
- **可委派列表**：`AgentListingContributor`（`TurnInputContributor`）在本 agent 可用 `agent_run` 时，每条用户消息把委派引导 `DELEGATION_GUIDE` + 可委派 agent 列表作为 note 追加（内容不变不重复注入），不写进工具描述以保持 schema 稳定。每行 `- 名字: description｜when_to_use(工具: …)`，工具按子 agent 实际会拿到的有效工具展开（与 `agent_run` 共用 `agents/delegation.py` 的规则）；与调用方上限相同时显示"与你相同,不含委派与会话控制类"，不逐个列。
- **实验**（Kimi k2.8，单次运行）：可用命令压缩的任务三种提示词都不委派；需大量阅读且可拆分的任务，无引导时模型先自己读一大圈再委派（用时约 2.5–3.5 倍），有引导时直接委派；引导放 note 与放系统提示词效果相当，采用 note。详见设计 §6。

尚未落地：`DelegationPolicy` 的 `allowed_agents` 白名单、权限/工具继承（inherit/override/intersect）、`isolation` 等字段仍仅声明，无执行点。

## harness 侧已落地的 fork 机制

async 模式的后台 run 使用同一套"快照 + fork + 回写"机制（`DefaultHarness._invoke`）：

1. 向真实 `ActiveContextStore` 追加 user 消息和 running `Placeholder` 之前，先做 pre-snapshot。
2. `_ForkedActiveContextStore` 以 `pre_snapshot + user_msg` 为种子创建私有 store；run 期间 assistant/tool/system 消息的 append 记入 `produced`，作为 PH 替换 payload。
3. run 走 forked context（`async_mode=False` 递归进同步路径）；结束时经 `write_queue`（`ReplaceItem`）或 `MainWriter` 回写真实 store，兜底直接 `replace_placeholder`。
4. 其他并发 run 的 Placeholder 保留在种子里，prompt 组装时由 `ph_expand.expand_placeholders` 展开为合成 user/assistant 对（"此问题正在后台处理中……"），本 run 永远看不到自己的 PH。

这是 async 后台 run 的上下文隔离方案，与 `GatewayAgentSpawner` 的 spawn fork（`ContextSDK.snapshot_for_fork` 拷贝父前缀到子 session，见上）是两套机制，不要混淆。
