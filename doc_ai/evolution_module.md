---
title: evolution module
type: doc
status: active
---

# evolution module

> 本文是 `src/xana/evolution/` 的实现真相（阶段四"迭代 episode + 元 agent"落地）。
> 设计方向的来源讨论见 [evolution](evolution.md)（设计共识文档，现 status: active 作为远期方向索引）。

## 代码锚点

- [contracts.py](../src/xana/evolution/contracts.py) — 数据合约（纯 dataclass / Literal / 常量）
- [ledger.py](../src/xana/evolution/ledger.py) — `Ledger` append-only 进化账本
- [changes.py](../src/xana/evolution/changes.py) — `ChangeWriter` 变更单写入口
- [episode.py](../src/xana/evolution/episode.py) — `EpisodeRunner` 状态机 + `EpisodeGatedSpawner` 门禁 + `EpisodeGateError`
- [meta.py](../src/xana/evolution/meta.py) — `MetaEvolutionService` 元 agent 定时唤醒
- [boot.py](../src/xana/runtime/boot.py) — 组装点（`boot()` 是唯一组合点）
- [scheduler.py](../src/xana/runtime/scheduler.py) — `ScheduledTaskDispatcher` marker 任务消费

## 模块定位

进化的可靠性由工程机制兜底，不靠模型自觉。本模块提供四块：数据合约、append-only 账本、episode 状态机（迭代生命周期 + 预算阈值 + 门禁）、元 agent 服务（定时唤醒 + prompt 组装 + 提案闭环）。全部是 concrete helper，**不抽 ABC**（checker 规则：contracts 只放纯数据，`EpisodeGateError` 这类 Exception 留在 episode.py）。

## contracts.py — 数据合约

| 合约 | 字段 / 取值 |
|---|---|
| `LedgerEntry` | `episode_id` / `goal` / `started_at` / `budget: dict` / `forked_from: str\|None` / `branches: tuple[str,...]` / `verdict: EpisodeVerdict\|None` / `delta_vs_base: float\|None` / `closure_tokens_used: int` / `continuation: tuple[str,...]` |
| `EpisodeVerdict` | `Literal["kept", "rolled_back", "abandoned"]` |
| `EpisodeState` | `Literal["declared", "running", "landing", "closing", "closed"]` |
| `EvalGroup` | `Literal["visible", "held_out"]` |
| `LintViolation` | `line_no` / `reason` |
| `ChangeBody` | 正文四段：`hypothesis` / `diff_summary` / `evidence` / `verdict`（默认 `pending`） |
| `EvalCase` / `EvalSet` | `case_id` / `prompt` / `group`；`EvalSet.visible()` / `held_out()` 两账视图 |
| 常量 | `GATE_REASONS = ("episode_landing", "episode_closing", "episode_closed")`、`CHANGE_SCHEMA_VERSION = "v1"`、`CHANGE_VERDICTS = ("pending", "accepted", "rejected")` |

`EpisodeGateError` 是 Exception 非 dataclass，按 checker 规则定义在 episode.py，不在 contracts.py。

## ledger.py — Ledger

- 存储：当前项目的 `~/.xana/projects/<id>/evolution/ledger.jsonl`（`default_evolution_home()` = `project_home() / "evolution"`，进化记录属于项目）；构造不传 path 时用默认 home。
- **append-only**：只有 `append` / `read_all` / `recent(n)` / `lint()`，刻意不提供 update/delete/rewrite（测试以 `hasattr` 断言锁死），是 `FileHistoryMirror` 同款机制的第三个实例。
- `append` 拒收无 verdict（None）与非法 verdict（不在 kept/rolled_back/abandoned 三值内）的条目，也拒空 `episode_id`。
- `lint()` 逐行校验：JSON 损坏行、缺 verdict、非法 verdict → `LintViolation` 列表。
- `recent(n)` 取近期 N 条（新在后），是元 prompt 组装的消费方。

## changes.py — ChangeWriter

- **门禁写入**：`ChangeWriter` 内部持有 `GatedEntryWriter(tree=tree, engine=engine, rules_dir="L2/changes")`（`evolution/change_gate.py`）：create 查重、supersede 取代链、frontmatter（status/evidence）。它原是 L2 规则的 memory_write 实现，记忆改为 `memory/` 后只剩变更单在用，移到本模块。
- 变更单目录固定为 `L2/changes`。
- schema（contracts `ChangeBody` 四段）：假设 → diff 摘要 → 证据指针 → 判决。`render_change_body` 渲染、`parse_change_body` 读回。
- `propose`：input 为 `path` / `hypothesis` / `diff_summary` / `evidence` [/ `supersedes`]，无 `supersedes` 走 create、有则走 supersede。
- `judge`：input 为 `path` / `verdict ∈ {accepted, rejected}`，校验目标文件存在且正文含"假设"段（否则判 bad schema），然后 refine 判决段、保留状态门禁。

## episode.py — EpisodeRunner

### 状态机

```
declared → running → landing → closing → closed
```

- **载体是 `RunRecord.budget` + `RunRecord.metadata["episode"]`**（§6.2：harness 升级，不是新 loop）。`declare()` 在 RunStore 建 `run_id = "episode-{episode_id}"`（已存在则 update_status 复用），`agent_name="meta"`，默认 `session_id="evolution"`（常量 `EPISODE_SESSION_ID`）。内存里的 `self._current` 只是镜像，权威在 RunRecord。
- 在途状态经 `_transition()` 写回 RunStore 的 metadata。

### 收尾预留与预算阈值

- `closure_estimate()` = 账本历史 `closure_tokens_used > 0` 的 **P75**（nearest-rank）；无历史用 `DEFAULT_CLOSURE_TOKENS = 20_000`。只统计 lint 语义上干净的账本（损坏行跳过）。
- **软阈值** = `budget.max_tokens − closure_estimate`（无 max_tokens 返回 None）。harness 每轮调 `record_usage(tokens)`：running 期累计 token 触软阈值 → 转 `landing`。
- **硬阈值**：累计 token ≥ `budget.max_tokens` → 强制转 `closing`，`record_usage` 返回 `"episode_hard_threshold"`。
- landing 期 `ensure_gate(action)` 拒绝 spawn / 分叉 / 新评估，抛 `EpisodeGateError`（reason ∈ `GATE_REASONS`）；机制兜底由 `EpisodeGatedSpawner` 包装 `AgentSpawner` 实现（boot 把 `gateway.agent_spawner` 换成 gated 版，并配 `GatewaySubagentDispatcher`）。在途评估等待完成，不中断。

### closing 两阶段 + 账本闭合（`close()`）

1. **canary 回归**：`{home}/canary/*.txt` 一文件一 prompt（缺省内置两条最小任务："用一句话说明你的目标。" / "回复 OK。"），任一失败 → 迭代后的 Agent 坏了。
2. **增益判决**：`eval_scores` vs `eval_base` 的 Δ（两集合同 case 各自取均值再相减，Δ vs base 不是 vs 上一版）；评估集两账（`evalset.json` 加载，`visible` / `held_out`），**held-out 分数不回流**给元 prompt（`render_eval_summary(include_held_out=False)`，落盘的 `last_eval.json` 同理）。

verdict 规则：canary 失败，或 Δ ≤ 0（有评估时）→ `rolled_back`；否则 `kept`。随后：

- verdict ≠ kept 且 declare 时给过 `doc_path` → **回滚 declare 时快照的文档内容**（harness 侧直写，不经过模型工具调用门禁）。
- `LedgerEntry` 落账本（含 `delta_vs_base` / `closure_tokens_used` / `continuation`——`continuation` 是 RunStore 里 `forked_from == 本 episode` 的子 episode id 列表，含僵尸闭合的）。
- 有评估分数时把 `last_eval.json` 落盘到 `{home}/`。
- 状态 → `closed`。

### 僵尸强制闭合与分叉 lineage

- `declare()` 前扫描 RunStore 里 state ≠ closed 的 episode → 强制闭合为 `abandoned` 并落账本（tokens 从 RunRecord.usage 估算）。
- 细化规则：全新迭代（`forked_from=None`）时当前 episode 也算僵尸、一并强闭；**分叉（`declare(forked_from=当前 episode)`）则保留父 lineage**——父 episode 与子 episode 都进 exclude 集，不被强闭。`close()` 闭合前也会清一次僵尸（防分叉子 episode 悬挂），exclude 只含自身。
- `note_branch(run_id)` 把分叉出的子 run 记入 `metadata.branches`。

### 文档冻结

`declare(doc_path=...)` 时快照文档内容；`close()` 时 verdict ≠ kept → 回滚快照（见上）。

## meta.py — MetaEvolutionService

- **幂等 install_cron**：默认 cron `0 */6 * * *`（每 6 小时唤醒），任务 prompt 是 marker 字符串 `"[xana-evolution-meta]"`，`session_id` 覆写为 `"evolution-meta"`（FileSchedulerService.session_id 是公有可变属性，cron 工具同款手法，用完恢复）。已存在同 marker 任务 → 返回 None 不重复建。无 scheduler → 返回 None。
- `owns(text)`：dispatch 回调用 marker 前缀识别任务是否归本服务。
- **assemble_prompt()** 固定四段 + 收尾指令：
  1. L3 当前计划（`TodoStore.load("evolution-meta")`，带状态图标渲染）；
  2. 账本近期 N 条（默认 `recent_n=10`，episode_id / verdict / delta / closure_tokens）；
  3. 当前 Agent 文档（优先 `~/.xana/agents/{agent}/AGENT.md` 全文；缺失则退回 gateway 里该 agent 的 `spec().system_prompt`，为 None 时写"xana 默认系统提示词"）；
  4. 上轮评估结果（`last_eval.json` + evalset，**只渲染 visible 集，held-out 分数不回流**）。
  收尾固定指令：按 episode 闭环提案（变更单假设→diff→证据 → 冻结 → 评估 → 判决），不要直接改 Agent 文档（被 policy 拦截）。
- **fire()**：到点唤醒——install_cron（幂等）→ `declare` 新 episode（`goal="迭代 agent {name}"`，`budget=DEFAULT_META_BUDGET={"max_tokens": 100_000}`，doc_path 指向当前 AGENT.md）→ `start()` → `assemble_prompt()` 经首个已注册 adapter 的 `gateway.submit(InboundMessage)` 注入。**每次 fire 用全新 session_id**（`evolution-{uuid8}`，不继承任何 worker 上下文），metadata 带 `target_agent` / `source="evolution_meta"` / `episode_id`；drain 任务吞异常。
- **propose_doc_change() 是元 agent 改 Agent 文档的唯一路径**：可选先经 `ChangeWriter.propose` 落变更单（`L2/changes` 门禁），然后 harness 侧直写 AGENT.md（模型侧裸 Write/Edit 被 protected_dir 拦截）。episode 闭合时按判决 keep / 回滚（文档快照机制见上）。

## harness 集成（usage 写点消费）

- `ChatAgent` 构造注入 `episode_runner`，每轮松散注入 `ctx.metadata["episode_runner"]`（与 `todo_store` / `budget_tracker` 同款）。
- harness turn loop 每轮 usage 写点累计：`_ep_tokens = (exact/est input) + (exact/est output)` → `record_usage()`。
- `record_usage` 返回非 None（即硬阈值）→ 记 `episode_exceeded`，发 `ProgressEvent(kind="episode_threshold", payload={reason})`（软阈值转 landing 时返回 None，只静默转态）。
- 收尾语义与 `budget_exceeded` 同款优雅落地：`episode_exceeded` 非 None → 不再开新轮 / 派发工具，带已产出文本落地；RunRecord.metadata 写 `episode_exceeded` 与 `finished_at`。

## Write/Edit 受保护目录（多目录）

`WriteTool` / `EditTool` 的 `protected_dir` 接受 `Path | tuple[Path, ...] | list[Path]`。boot 注入双目录：

- `{project_root}/memory`（项目记忆，唯一写入口 `memory_write`）；
- `~/.xana/agents`（用户级 Agent 文档目录，元 agent 的 `agents_root`；唯一变更路径 = episode 提案→评估→判决闭环。项目级 `<project_root>/.xana/agents` 同样对 Write / Edit 受保护，写入口是 `agent_define`）。

目标路径落在任一受保护目录内 → `decision="deny"`。详见 [tools_permission_sandbox](tools_permission_sandbox.md)。

## serve 集成（marker 任务 dispatch）

`runtime/scheduler.py` 的 `ScheduledTaskDispatcher`（boot 组装时注入 `meta_service`）：到点任务先过 `meta_service.owns(prompt)`，命中 marker → `meta_service.fire()`（产出 episode + 全新窗口注入元 prompt），不走普通定时任务的独立子会话流程。boot 里 `MetaEvolutionService` 的 `agent_name` 取 FileAgentRegistry 第一个 spec 的组件名（缺省 `"chat"`），`agents_root = ~/.xana/agents`，`todo_store` 与 chat 共用。详见 [runtime_scheduler](runtime_scheduler.md)。

## 不变量

- 账本 append-only：无 verdict 不落盘；lint 能抓缺 verdict / 非法 verdict / 损坏行。
- 变更单的门禁（查重 / supersede / refine）只在 `change_gate.py` 一处。
- 软阈值只拒新支线（spawn / 分叉 / 新评估），不杀在途；硬阈值才强制 closing。
- held-out 评估分数不回流给元 prompt（两账纪律贯穿 close / last_eval.json / assemble_prompt）。
- 元 agent 不能裸改 Agent 文档：protected_dir 拦截 + propose_doc_change 唯一路径。
- 每次 fire 全新 session，不继承 worker 上下文。

## 未实施（已知缺口）

- **着陆期 compaction 换保留策略**：landing 期仍沿用普通聊天式 compaction，尚未切换为面向收尾的保留策略（设计共识中的交接 checkpoint 方向未落地）。
- replay simulator、后台 bash task 系统等远期方向见 [evolution](evolution.md)"仍未实现"。

## 相关模块

- [evolution](evolution.md) — 设计方向共识（本模块的需求来源）
- [memory_rules_gate](memory_rules_gate.md) — 项目记忆写入（memory_write）
- [agents_registry](agents_registry.md) — FileAgentRegistry（元 agent 的 agent_name 来源）
- [agent_adapter_gateway](agent_adapter_gateway.md) — gateway.submit / AgentSpawner（门禁包装对象）
- [tools_permission_sandbox](tools_permission_sandbox.md) — protected_dir 门禁
- [cli_serve](cli_serve.md) — scheduler dispatch 接线
- [harness_sdk](harness_sdk.md) — usage 写点 / 优雅落地语义
