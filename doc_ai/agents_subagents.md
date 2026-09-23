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

## 职责

定义 subagent 委派的请求/结果契约与委派策略，以及 `SubagentDispatcher` 分发接口。

## 契约

- `SubagentRequest` — `parent_session_id` / `parent_agent` / `parent_run_id` / `target_agent` / `task` / `new_session_id`，外加形态控制：`mode`（`inline / background / isolated`，默认 `isolated`）、`allowed_tools`、`fork_context_messages`、`inherit_cwd`（默认 `True`）、`isolation`、`metadata`。
- `SubagentResult` — `parent_session_id` / `child_session_id` / `target_agent` / `ok` / `result` / `transcript_ref` / `metadata`。
- `DelegationPolicy` — `allow_subagents`（默认 `True`）、`max_depth`（默认 1）、`allowed_agents`、权限与工具继承方式（`inherit / override / intersect`，默认 `intersect`）、`context_fork`（`none / messages / full`，默认 `none`）、后台/prompt 相关开关。

## 当前状态

`SubagentDispatcher` 已有首个实现 `GatewaySubagentDispatcher`（agent_adapter/gateway.py，boot 构造注入，同时挂 `gateway.subagent_dispatcher` 和 `BootResult`）：

- **深度门禁**：父 session depth ≥ `DelegationPolicy.max_depth`（默认 1）时拒绝，返回类型化错误 `SubagentResult(ok=False, metadata={"error": "max_depth_exceeded", "max_depth", "depth"})`，不抛异常。
- **lineage 权威写点**：dispatcher 持有完整 `SubagentRequest`，通过时先落库子 `SessionRecord`（`parent_session_id` / `parent_agent` / `parent_run_id` / `depth=parent.depth+1`，cwd 继承父），再调 spawner。
- **派发**：调 extension_api `AgentSpawner.spawn(target_agent, task, new_session_id, parent_run_id, fork=fork_context_messages)`，成功返回 `SubagentResult(ok=True, transcript_ref=f"runs/{run_id}.jsonl", metadata={"run_id": run_id})`。

extension_api `AgentSpawner` 的首个实现 `GatewayAgentSpawner` 与 dispatcher 同文件：spawn 走 gateway `_submit` 同款链路（InboundMessage 带 `spawn_run_id` / `spawn_background` / `target_agent` metadata，fire-and-forget 后台排干），写/补 `SessionRecord.parent_*`；`fork=True` 时经 `ContextSDK.snapshot_for_fork` 把父上下文快照拷贝为子 session 初始前缀——父缓冲只读共享（子不写入父）、子 run 只 append 私有后缀、结果不做上下文物理合并（回流走后续阶段的账本/变更单，防分支污染）。fork 的父 session 从子 SessionRecord.parent_* 解析，无 lineage 记录时退回自身 session（退化自拷贝）。详见 [agent_adapter_gateway](agent_adapter_gateway.md)。

尚未落地：`DelegationPolicy` 的 `allowed_agents` 白名单、权限/工具继承（inherit/override/intersect）、`isolation` 等字段仍仅声明，无执行点。

## harness 侧已落地的 fork 机制

async 模式的后台 run 使用同一套"快照 + fork + 回写"机制（`DefaultHarness._invoke`）：

1. 向真实 `ActiveContextStore` 追加 user 消息和 running `Placeholder` 之前，先做 pre-snapshot。
2. `_ForkedActiveContextStore` 以 `pre_snapshot + user_msg` 为种子创建私有 store；run 期间 assistant/tool/system 消息的 append 记入 `produced`，作为 PH 替换 payload。
3. run 走 forked context（`async_mode=False` 递归进同步路径）；结束时经 `write_queue`（`ReplaceItem`）或 `MainWriter` 回写真实 store，兜底直接 `replace_placeholder`。
4. 其他并发 run 的 Placeholder 保留在种子里，prompt 组装时由 `ph_expand.expand_placeholders` 展开为合成 user/assistant 对（"此问题正在后台处理中……"），本 run 永远看不到自己的 PH。

这是 async 后台 run 的上下文隔离方案，与 `GatewayAgentSpawner` 的 spawn fork（`ContextSDK.snapshot_for_fork` 拷贝父前缀到子 session，见上）是两套机制，不要混淆。
