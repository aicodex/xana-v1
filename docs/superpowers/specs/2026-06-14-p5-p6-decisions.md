# P5 + P6 缺口决议 —— Cancel/Resume + Async-aware Compact

> 本文沉淀 ROADMAP `2026-06-13-async-run-ROADMAP.md` §Phase 5、§Phase 6 中"目标段落已写但未拍板"的空白点。
> 写 P5 / P6 实施 plan 时以此文为准;与 `ASYNC_RUN_DESIGN.md`(/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md)冲突时以本文为准。
>
> 上游 spec:
> - 数据/渲染:`2026-06-14-async-chat-design.md`、`2026-06-14-ph-driven-rendering-design.md`
> - 路线图:`../plans/2026-06-13-async-run-ROADMAP.md`

---

## P5 — Cancel / Resume / 重启恢复

### P5.1 — "写入工具 2s 等待窗口"的判定边界

复用现有 `ToolSpec.read_only`,**不新增字段**。在 `ToolSpec.read_only` 字段定义处加注释:

> `read_only` 也用于取消语义 —— `True` = cancel 时立即 abort;`False` = cancel 时等 2s 宽限,完成则正常写,超时强杀。

不提前过度设计;后续若需更细粒度再加 `cancellable_immediately`。

### P5.2 — 强杀后的 synthetic warning

写入 `tool_result.content`(LLM 可见),**统一文案**,显式要求模型不要自动重试:

```
[工具执行已被用户取消(超过 2 秒宽限期后强制终止)。该操作可能已部分完成 —— 切勿自动重试,半完成状态下重试可能让目标变得更糟。请等待用户的下一步指示。]
```

本期统一;后续若有工具想自定义,改成"工具可覆盖,默认用上文"。

### P5.3 — resume = fork 新 run 时原 run JSONL 处置

文件系统层面隔离:`runs/{run_id}.jsonl` → `runs/{run_id}.aborted.jsonl` 改名。新 run 写新文件,不引用旧。

### P5.4 — `/resume` 列表筛选规则

`exit_reason` 枚举与是否进 /resume:

| exit_reason | 进 /resume | 含义 |
|---|---|---|
| `user_cancel` | ✅ | 用户主动 ctrl-C / 命令取消 |
| `tool_kill_timeout` | ✅ | 写入工具 2s 宽限超时被强杀 |
| `shutdown_graceful` | ✅ | SIGTERM 来了正在跑,正常 flush 完 |
| `shutdown_force` | ✅ | 进程崩了/kill -9 / flush 超时,下次启动扫到 |
| `network_error` | ❌ | LLM 请求网络挂了 |
| `llm_timeout` | ❌ | LLM 流超时 |
| `session_lost` | ❌ | session 文件损坏 |
| `tool_error` | ❌ | 工具自己抛 Exception(已是失败结果落 main) |
| `unknown` | ❌ | 保守不进 |

### P5.5 — 启动扫 `runs/` 状态冲突仲裁

同 `run_id` 同时存在 completed marker(A)和"看起来跑一半"(B)时:

**A 赢 —— 有 completed marker 就信,替换 PH,忽略 B。**

理由:marker 是显式信号,B 仅是缺失证据;时间戳判定引入额外失败模式。

### P5.6 — graceful shutdown 2s flush 超时

flush 没完成 → 下次启动按"跑一半"(B)处理,`exit_reason=shutdown_force` 进 /resume,用户选续跑。

### P5.7 — `triggers_turn` 通用字段?

**不引入。** `/compact` 在菜单拦截器里特判分流,走自己的"detach + 子分支 compact"路径。其它命令一律按"纯状态操作"处理(无 stop 按钮)。

### P5.8 — `/compact` 取消语义

**2026-06-15 更新**:**compact 可取消**,且是当前**唯一**带专用 cancel 路径的特殊命令。其他命令走通用工具 cancel(P5.1 / 2s 宽限)。

实现:compact 卡保留"取消"按钮 → 触发 `/compact_cancel <run_id>` 隐藏命令 → 置 `cancel_event` → 流式 summary 生成被 `_CompactCancelled` 中断 → 不写回 main → finalize PH 卡。

历史:原 ROADMAP §Phase 5 写"特殊 turn 走 cancel_path",`ASYNC_RUN_DESIGN.md §Compact L519` 改为"本期不可取消" → 本 spec 2026-06-14 沿用 → 2026-06-15 改回可取消。理由:compact 是长流操作,用户取消语义清楚(放弃这次压缩),不存在工具半执行的 destructive 状态。

### P5.9 — synthetic warning 文案

见 P5.2,已固定中文文案。

---

## P6 — Async-aware Compact

### P6.1 — fork_before 精确定义 + PH 存储改两节点

ROADMAP/ASYNC_RUN_DESIGN.md 已写 `fork_point = 最早 running PH 位置`。本期补:

**PH 存储改两节点**(改 memory submit ritual):
- 之前:main append `Placeholder(run_id, label, status, jsonl_path)` 单节点,LLM prompt 期由 `ph_expand` 用 label 合成 `(user, assistant)` 对。
- 改后:main append `MemoryMessage(role=user, content=label)` + `Placeholder(...)` 两节点。
- `ph_expand` 改读 main 里现成 user msg,不再从 label 反推。

**fork_before 切点**:卡在最早 running `Placeholder` 节点**之前** —— user 半被压进 summary,Placeholder 留在压后区。

理由(CC 对齐):auto-compact 把当前 user msg 也压进 summary,模型从 summary 自然续答,不需要 re-send user;同理别人 PH 的 user 也进 summary 才完整。

⚠️ 接口改动 —— P6 实施期需走 `feedback_interfaces_locked.md` 审批:
- `memory/interfaces.py` `ActiveContextStore.append` 调用方变 + 新增/调整 submit ritual API
- `harness_sdk/ph_expand` 改读 main 而非从 label 合成

### P6.2 — per-run 去重 key

key = `run_id`。同 run_id 内只触一次 fork_before main compact。

### P6.3 — main compact 重复触发规避

**全局规则**(比 P6.2 强):**fork_point 未前移过,任何 run 都不再触发 main compact**(反复压自己摘要无意义)。

状态:`ActiveContextStore` 记 `last_main_compact_fork_point_msg_id`。任何 run 想触 main compact 时,先比对:
- fork_point > last_main_compact_fork_point_msg_id → 触发 main compact
- 否则 → 跳过 main compact,改触发**子分支 compact**(只压当前 run 自己执行过程)

**子分支 compact 触发时打观测日志**:

```python
logger.warning(
    f"sub-branch compact in run {run_id}; "
    f"early PH {early_run_ids} blocks main fork_point"
)
```

频次自然由 token 阈值控制;不刷屏。原 ROADMAP §Phase 6 "前置 PH 超 30min warning"作废 —— 时间不是好信号,**子分支 compact 触发**才是。

### P6.4 — `CompactReference` 指针格式

**原样抄 CC**:summary 文本末尾自然语言提示"完整细节见 main.jsonl",**不加结构化 metadata**。

### P6.5 — 子分支 compact 频次

不限次,纯按 token 阈值触发。每次只压自己 run 内部已产出,不动 main。

### P6.6 — 手动 `/compact` 在 main 里存什么

手动 `/compact` 分两种情况,语义差异:

**Case 1 —— 有后台 PH 存在(含"detach 当前 fg 转 bg"路径)**

- fork_point < main 末尾
- 与 auto-compact 完全一致:**不为 compact 创建任何节点**(不追加 user,不追加 Placeholder),直接 `replace_range(fork_before, summary)`
- 例:`[msg0, user_A, PH(A), user_B, PH(B)]` → `[summary, PH(A), user_B, PH(B)]`

**Case 2 —— 当下无任何后台 PH**

- fork_point == main 末尾
- 末尾**只追加 `Placeholder(compact_run)`,无 user 节点**(/compact 命令本身不进 main)
- 核心:**弄个分叉防新插队** —— compact 跑期间若用户发新消息,新消息追加在 PH 之后,自然落在 fork_point 之后,不被这次 compact 吃掉
- 完成时把 PH 替换为 summary message
- 例(无插队):`[msg0,...,msgN]` → `[summary]`;有插队:`[summary, user_X, PH(X), ...]`

**规则总结:**
- 两节点规则(MemoryMessage(user, label) + Placeholder)**只适用普通 chat run**(用户问题驱动)
- compact run **永不追加 user 节点**(/compact 不进 main)
- Case 判定:触发时 fork_point < main 末尾 → Case 1;否则 → Case 2

### P6.7 — compact 卡折叠态内容

折叠块默认**只显 summary 文本**,不带"原 N 条已压缩"提示。

### P6.8(已隐式定)— compact 与 PH 替换并发仲裁

ASYNC_RUN_DESIGN.md §Compact L489/L502/L507 已写:都走 write queue,**串行化 FIFO**。无开放问题。

---

## 接口改动清单(P5+P6 合并,实施期审批)

按 `feedback_interfaces_locked.md`:

- **`tools/contracts.py`** — `ToolSpec.read_only` 仅改注释,**字段不动**。
- **`state/contracts.py`** — `RunRecord.exit_reason` 字段(若不存在则新增),值域见 P5.4 表。
- **`state/contracts.py`** — `RunStore.list_unfinished` 过滤行为:按 P5.4 表筛选 exit_reason。
- **`memory/contracts.py`** — `Placeholder` 字段不变。
- **`memory/interfaces.py`** — `ActiveContextStore`:
  - `append_placeholder` 调用方按 P6.1 两节点改造(submit ritual 层负责,接口本身可能不动)
  - 新加 / 已有 `replace_range(msg_ids, summary_message)`(ASYNC_RUN_DESIGN 已写)
  - 加 `last_main_compact_fork_point_msg_id` 状态(实现私有,接口可不暴露)
- **`harness_sdk/ph_expand`** — 改读 main 现成 user msg。
- **adapter** — compact 卡 stop 按钮拿掉(P5.8 / 不可取消)。
