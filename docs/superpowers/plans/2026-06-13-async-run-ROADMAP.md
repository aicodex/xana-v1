# 异步 Run + 占位符 —— 分阶段实施路线图

> **怎么用这份文档**:这是把 `ASYNC_RUN_DESIGN.md`(设计源,在 `/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`)落地到 xana 的**分阶段拆分**。
> 每个 phase 单独写一份实施 plan(`docs/superpowers/plans/2026-06-13-async-run-phaseN-*.md`)。
> **写下一个 phase 的 plan 时,读这份 ROADMAP + ASYNC_RUN_DESIGN.md + 当时已 commit 的代码**,因为后面阶段依赖前面阶段的产物,代码会演进,不能一次把所有 plan 写死。

**设计源**:`/home/xzp/project/best_agent/ds_review/ROADMAP/ASYNC_RUN_DESIGN.md`
**总目标**:让 xana 支持「用户打断 → 旧问题后台继续跑」的异步 Run 模型 + 占位符替换 + 可恢复的会话/Run,同时保留同步(阻塞)模式。

---

## 现状基线(2026-06-13 探查结论)

**已存在、可直接复用:**
- 三层接口纪律(contracts/interfaces/implementation)成熟。
- `state/`:`RunRecord`(status: running/paused/cancelled/failed/completed、`Checkpoint`(8 个检查点)、`UsageSnapshot`、`pause_reason`/`abort_reason`)、`RunStore`(create/get/update_status/checkpoint/list_unfinished/list_by_session)、`SessionRecord`/`SessionStore`、`ResumePolicy`/`ResumeDecision`。**仅 InMemory 实现,无 File 实现。**
- `memory/`:`MemoryMessage{role, content, metadata}`、`ActiveContext`、`HistoryRow`(kind: user/assistant/tool/system/compact)、`ActiveContextStore` / `HistoryMirror` / `MemoryReplayer` ABC。**仅 InMemory,无 JSONL 落盘,replay 无生产实现。**
- `harness_sdk/`:`DefaultHarness._invoke()`(同步阻塞流式 query loop)、`HarnessContext`(frozen,含 active_context_store/history_mirror/state_repository/abort/config_snapshot)、`HarnessEvent`(turn_started/model_delta/tool_started/tool_finished/paused/cancelled/final/error)、`AbortSignal`、compact.py(同步内联,boundary marker=`对话已压缩`)。
- `session_config/`:`SessionConfig`(model/effort/streaming/thinking/search_provider/project_root/metadata),`FileSessionConfigStore`(JSON 原子写,`~/.xana/sessions/{sid}.json`)。**`/thinking`、`/streaming` 已支持无参 toggle。**
- `agent_adapter/`:`DefaultCommandRouter`(/help /clear /compact /model /effort /streaming /thinking /search_provider /cancel /project /compact_cancel),`CommandResult`(payload: buttons/linkify_commands/silent/run_id/thinking),命令卡预建(`gateway` 发 `command_progress` + `run_id=cmd-{sid}-{n}`),卡片层 per-run `_TurnState` + `_SessionBuffer.turns`(OrderedDict,保留最近 4)+ run_id 事件路由 + feishu/telegram 同卡编辑。
- `tools/`:`ToolSpec.read_only`(cancel 时区分只读/写入工具)、`ToolResult`(failure_kind/killed 等)。

**关键缺口(本工程要补):**
- ❌ 任何 JSONL 落盘(main history、run history)。
- ❌ write queue 单写者 consumer。
- ❌ `Placeholder` 概念。
- ❌ 双模(async_mode)开关。
- ❌ 重启恢复 / `/resume` / 未完成 run 续跑。
- ❌ 后台 run 的 tab/切换 UI。
- ❌ async 感知的 compact(range-replace)。

---

## 阶段总览

```
P1 持久化地基 ── P2 单写者+Run/PH 模型 ── P3 双模+/async+后台run ── P4 后台run卡片UX
                                          └────────────────────── P5 Cancel/Resume/恢复
                                          └────────────────────── P6 async 感知 compact
```

依赖:P2 依赖 P1;P3 依赖 P2;P4/P5/P6 依赖 P3(P5/P6 也重度依赖 P2 的 run JSONL + 队列)。

---

### Phase 1 —— JSONL 持久化地基 + replay 恢复 ✅ 本次开工

**目标**:main history 落 `sessions/{session_id}.jsonl`;`FileHistoryMirror` 追加写 + 真 `MemoryReplayer` 重建 `ActiveContext`;boot/serve 接入;重启后 replay 恢复历史 + CC 风格中断检测(末尾是 user/tool_result → 注入「Continue…」)。

**范围**:
- `FileHistoryMirror`(实现已有 `HistoryMirror` ABC):append `HistoryRow` → JSONL 行;`replay()` 读回。
- 真 `MemoryReplayer.rebuild_active_context`(实现已有 ABC):JSONL → 重建 `ActiveContext`。
- 目录布局:`{session_dir}/sessions/{sid}.jsonl`(为 P5 的 `runs/{run_id}.jsonl` 预留同级目录)。
- boot() 注入 File 实现(InMemory 与 File 并存,boot 选择;serve 用 File,测试可用 InMemory)。
- 启动恢复:replay main.jsonl → 重建 context;中断检测注入 Continue。
- JSONL 序列化忠实 round-trip `MemoryMessage{role, content, metadata}`(thinking/tool 信息在 metadata,原样存)。

**纯 sync,行为 = 现状 + 持久化。无 Placeholder、无 queue、无 async。**

**接口改动**:**无**。全是给已有 ABC(`HistoryMirror`/`MemoryReplayer`/可能 `ActiveContextStore`/`SessionStore`)补文件实现 + boot 组装。

**独立价值**:历史跨重启不丢——本身就有用,且是后面一切的底座。

---

### Phase 2 —— write queue 单写者 consumer + Run/Placeholder 数据模型(仍 sync)

**目标**:引入唯一写 main history 的 consumer 循环(`asyncio.Queue` + 单消费者);`Placeholder` 数据契约;`RunStore` 落盘 `runs/{run_id}.jsonl`;sync 模式下所有 main 写入改走队列(行为不变,为 async 铺路)。

**范围**:
- write queue + consumer(序列化所有 main 写入:append、后续的 PH 替换、compact range-replace)。
- `Placeholder` 契约(`run_id`/`label`/`status`/`jsonl_path`),`ActiveContextStore` 支持 message-or-placeholder。
- `FileRunStore`(实现已有 `RunStore` ABC)+ per-run JSONL writer + `run_completed` marker。
- run_id 生成规范(参考 scheduler 的 `uuid4().hex[:8]`)。

**接口改动(需先问用户)**:
- 新增 `Placeholder` 契约 → 放 `state/contracts.py` 还是 `memory/contracts.py`?
- write queue / consumer 是否需要新 ABC?放哪个 `interfaces.py`?
- `ActiveContextStore` 是否需要 `append_placeholder` / `replace_placeholder` / `snapshot` 方法(改 `memory/interfaces.py`)?

---

### Phase 3 —— 双模 Harness(sync/async)+ /async 切换 + 后台 run

**目标**:`HarnessContext.async_mode`;ChatAgent 覆盖为 async;`SessionConfig.async_mode` 持久化 + `/async` toggle(on→off 有 running PH 时拒绝);async 下 snapshot+fork 独立上下文 + 追加 PH + `asyncio.create_task` 后台跑 + run JSONL → queue → 替换 PH;用户发新消息打断 → 当前 run 退后台。

**接口改动(需先问用户)**:
- `HarnessContext` 加 `async_mode`(改 `harness_sdk/contracts.py`)。
- `SessionConfig` 加 `async_mode`(改 `session_config/contracts.py`)。
- `/thinking`、`/streaming` 已是 toggle;`/async` 同规范。

**默认值链**:`SessionConfig` > `ChatAgent`(覆盖 async) > `Harness 默认`(sync)。子 agent 默认 sync。

---

### Phase 4 —— 后台 run 卡片 UX:多 run 切换 + 底部 tab + 完成弹卡

**目标**:底部 tab 只在后台 run ≥ 1 时显示;点 tab 切换当前卡(当前退后台换位);后台 run 完成 → 推纯回答卡(只回答+按钮,不抢焦点);看着的 run 完成 → 自然过渡;占位符不渲染。

**复用**:现有 `_TurnState`/`_SessionBuffer.turns`(OrderedDict)/run_id 路由/feishu collapsible_panel/telegram blockquote。

**接口改动**:可能 adapter 层小扩展(tab 渲染/切换事件),届时评估。

---

### Phase 5 —— Cancel / Resume / 重启恢复未完成 run + /resume

**目标**:turn 级 cancel(无工具→止 LLM;只读工具→立即 abort,resume=重跑该工具;写入工具→等 2s,完成则正常写,超时强杀+synthetic warning,resume=fork 新 run);冻结回写 main(partial+打断原因+warning);`/resume` 列表 + 恢复(防重入);启动扫 `runs/`(completed 未消费→替换 PH;跑一半→续跑或标记中断)+ main 中断 PH;graceful shutdown(SIGTERM 先 cancel 后台 run ≤2s 再 flush);非用户异常(network/timeout/session_lost)不入 /resume。

**复用**:`RunStore.list_unfinished`、`Checkpoint`、`ResumePolicy`/`ResumeDecision`、`ToolSpec.read_only`、`AbortSignal`、`sync.py` 的 `_terminate`。

---

### Phase 6 —— async 感知 compact

**目标**:compact 走 write queue 做 `replace_range`(按 msg_id 不按索引,避免与 PH 替换并发冲突);fork_before = 最早 running PH 位置,每次全量覆盖;每个 run 只触发一次 fork_before compact;手动 `/compact` = 开一个无 user 问题的空 run(同子分支 compact 流程);前置 PH 超 30min 未完成 → warning 日志(纯观测);post-compact 消息引用 main.jsonl(抄 CC)。

**复用**:现有 `compact.py`/`compact_prompt.py`、boundary marker、`build_post_compact_context`、`generate_summary`(已支持 on_progress)。

---

## 跨阶段不变量

- 严守三层:新接口只能进 `*/interfaces.py`,数据进 `*/contracts.py`,实现不定义新 ABC。**改任何 contracts/interfaces 字段前先问用户。**
- `boot()` 是唯一组合点。
- main.jsonl 是唯一真相源;run JSONL 完成后合入 main 并删除。
- 只有一个锁 `session._lock` 保护 main 读写;run 写自己的 JSONL 无锁。
- 每个 phase 落地后 commit + `~/.xana/bin/xana sync`,再写下一个 phase 的 plan(读当时已 commit 代码)。
- 测试由独立 git 仓管理(`tests/`),主仓忽略。
