---
title: agent adapter gateway
type: doc
status: active
---

# agent adapter gateway

## 上级模块

- [agent_adapter](agent_adapter.md) — 模块设计

## 代码锚点

- [contracts.py](../src/xana/agent_adapter/contracts.py) — `SessionBinding` / `AdapterHealth` / `InflightRun` / `TaskPanelRow`
- [interfaces.py](../src/xana/agent_adapter/interfaces.py) — `Adapter` / `AgentGateway` ABC
- [gateway.py](../src/xana/agent_adapter/gateway.py) — `DefaultAgentGateway` / `GatewayAgentSpawner` / `GatewaySubagentDispatcher`
- [boot.py](../src/xana/runtime/boot.py) — 组装点

## 职责

gateway 是 adapter 与 agent 之间的唯一组合点：管理 adapter 注册、session 绑定、入站消息的生命周期（前台/后台 run、取消、续跑），并把 agent 事件经 adapter 渲染后回传。

## 契约

- `SessionBinding` — adapter_id / external_thread_id / session_id / user_id / metadata。
- `AdapterHealth` — adapter_id / status（starting/healthy/degraded/stopped/failed）/ details。
- `InflightRun`（非 frozen）— per-run 追踪：`session_id` / `run_id` / `label` / `task` / `is_attached`。`is_attached=True` 表示该 run 是 session 当前前台 run；新 submit 会把现有 attached run 翻成 False（转后台，不取消）。
- `TaskPanelRow`（frozen）— 任务面板一行：`run_id` / `session_id` / `label` / `status` / `attached` / `duration_s` / `live`。`live=True` 表示该 run 在 gateway `_inflight` 里（在途），`live=False` 表示只来自持久化 RunStore（detached 遗留 / 已完成）。
- `Adapter` ABC — `start` / `stop` / `receive()` / `render(event)` / `bind_session(binding)` / `health()`；adapter 可选暴露 `note_progress`（收 progress/卡片控制事件）、`rotate_session`（/clear 用）。

## DefaultAgentGateway

构造注入 `agents: dict[str, Agent]` 和 `AgentRequestRouter`；`run_store` / `run_jsonl_writer` 由 boot 在构造后注入（P5 wiring），供 /resume 和 cancel_all 使用。

### submit 生命周期（`_submit`）

1. 未知 adapter → 直接渲染错误消息返回。
2. 斜杠命令短路：先问 command router。除 `/compact` `/compact_cancel` 外，命令先预建一张卡（`command_progress` note + 分配 `cmd-{sid}-{seq}` run_id 经 metadata 传给 router）。结果按 payload 分流：silent 直接结束；requires_detach 先 detach 前台再渲染；triggers_turn（/compact）落入普通 turn 流程；其余渲染命令结果卡并返回。
3. `detach_foreground(session_id)`：现前台 run 转后台（不 cancel），向所有 adapter 发 `run_detached` note 刷 tab。
4. 新建 `InflightRun`（run_id = `run-{sid}-{seq}-{uuid8}`；spawn 路径复用 metadata 预分配的 `spawn_run_id` 以维持 lineage 可寻址；label 取输入前 40 字），插入 `_inflight[sid]` 头部（倒序）。`metadata["spawn_background"]=True` 时 `is_attached=False`（spawn 的子 run 是后台子任务，不抢占用户前台）。
5. P5：handoff 前注册该 run 的 `AbortSignal` 存入 `_abort_signals`；经 metadata 传 `gateway_run_id` 和 `abort_signal` 给下游（保证 harness 事件 run_id 与 adapter 渲染状态 key 一致、cancel 能原地翻转在跑 turn）。
6. `router.route` → 取 agent：优先 `metadata["target_agent"]`（spawn 携带的目标 agent），否则 `request.target_agent`，再否则 router 默认路由（未知 agent 渲染错误）→ 发 `run_started` note（PH 状态机依赖它初始化卡片）→ 迭代 `agent.send`，每个事件经 `adapter.render` 后 yield。
7. finally：从 `_inflight` 移除；先读 `abort_signal.aborted` 再 pop 信号（顺序不能反）；发 `ph_replaced` note；若 run 期间被 detach，补发 `run_completed_bg`。

### attach / detach 语义

- `detach_foreground(sid)`：只翻第一个 attached run，通知所有 adapter。
- `attach(sid, run_id)`：目标不存在则 noop；先自动 detach 其它 attached run（各发 `run_detached`），再把目标置 attached 并向各 adapter 发 `attached` note（带 label）。run_id 未知/已被替换时不报错——这是 `/attach` 菜单点击的容错设计。
- attach/detach 的 `is_attached` 变化经 `_mirror_attached` 镜像进 `RunRecord.metadata["attached"]`（attach/detach 两侧都写；记录尚不存在如 spawn/启动竞态时静默跳过，展示侧缺省按 attached 处理）。

### list_inflight（任务面板数据面）

`list_inflight(session_id=None) -> tuple[TaskPanelRow, ...]`：合并 `_inflight` 活体状态 + RunStore 持久状态。持久侧用 `run_store.list_all()`（或 `list_by_session`，当 session_id 给定），时长取 `metadata["started_at"]/["finished_at"]`；活体 run 覆盖同 run_id 的持久行（在途以活体为准：status=running + 实时 attached）。`/tasks` 命令的消费口，见 [agent_adapter_commands](agent_adapter_commands.md)。

### cancel / cancel_all

- `cancel(sid, run_id=None)`：置 `_cancel_flags[sid]`；有 run_id 则翻转该 run 的 AbortSignal，无 run_id 则翻转本 session 全部 inflight 信号（cancel_all 路径）；随后调每个 agent 的 `cancel`；有 run_store 时把 RunRecord `exit_reason` 标为 `user_cancel`。
- `cancel_all()`：SIGTERM 入口，对所有 inflight run 逐个 cancel 并覆盖 `exit_reason=shutdown_graceful`。

### resume

`resume(sid, run_id)`：`run_jsonl_writer.rename_to_aborted(run_id)` 把原 run JSONL 改名 `.aborted.jsonl`，再取 RunRecord 的 input text 走正常 `_submit` fork 新 run。`_resume_inflight` 集合防同 run 并发续跑；无 adapter 时记 warning 返回。

### subscribe

`subscribe(adapter_id, session_id)` 直接 `raise NotImplementedError("subscribe is not used by the CLI flow in v1")`——v1 未实现。CLI 流程是 `submit()` 拉取渲染流；daemon bridge 的 SSE 式订阅由 adapter 侧 ring buffer + subscriber queue 自行实现，不经 gateway。

### 杂项

`has_background()`（有无未完成的 bg task）、`is_cancel_requested` / `clear_cancel`（session 取消标记）、`register_binding`（bindings dict，按 session_id 存）。

### spawn 元数据键（_submit 识别）

`_submit` 识别三个 inbound metadata 键，是 `GatewayAgentSpawner` 与普通入站共享同一链路的挂钩：

- `spawn_run_id`：spawn 预分配的 run_id（复用而非自分配，维持 lineage 可寻址）。
- `spawn_background`：置 True 时新 run `is_attached=False`（后台子任务，不抢占前台）。
- `target_agent`：目标 agent 名，优先于 router 默认路由。

另透传 `parent_run_id`（经 harness 写入子 RunRecord）与 `gateway_run_id` / `abort_signal`。

## GatewayAgentSpawner / GatewaySubagentDispatcher

gateway.py 同文件的 extension_api `AgentSpawner` 与 agents `SubagentDispatcher` 首个实现（详见 [agents_subagents](agents_subagents.md)）：

- `GatewayAgentSpawner.spawn(...)`：写/补 `SessionRecord.parent_*`/`depth`；`fork=True` 时经 `ContextSDK.snapshot_for_fork` 把父上下文快照拷贝为子 session 初始前缀（父缓冲只读共享、子 run 只 append 私有后缀、结果不做上下文物理合并）；构造 InboundMessage 带上述 spawn metadata，fire-and-forget 排干 `_submit`，立即返回 run_id。未知 agent 抛 `LookupError`。
- `GatewaySubagentDispatcher.dispatch(request)`：`DelegationPolicy.max_depth` 门禁——父 session depth 超限时返回类型化错误 `SubagentResult(ok=False, metadata["error"]="max_depth_exceeded")`，不抛异常；通过时它是 lineage 权威写点（落库子 SessionRecord 完整 parent_*/depth），再调 spawner。

## 组装

boot 创建 gateway（agents 先空、router 固定 `DefaultAgentRequestRouter("chat")`），随后 `gateway.agents["chat"] = chat_agent` 并注册 command router；channel adapter（cli daemon bridge、telegram、feishu）由 `xana serve` 路径 `gateway.register_adapter(...)` 注册。boot 还在构造后注入 `gateway.run_store` / `gateway.run_jsonl_writer`（P5 wiring），并构造 `GatewayAgentSpawner` / `GatewaySubagentDispatcher` 挂到 `gateway.agent_spawner` / `gateway.subagent_dispatcher` 与 `BootResult`。
