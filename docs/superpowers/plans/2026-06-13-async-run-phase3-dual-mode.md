# Phase 3 —— 双模 Harness(sync/async)+ /async 切换 + 后台 run

> 依赖 Phase 1(FileHistoryMirror + replay)、Phase 2(MainWriter 单写者 queue + Placeholder 契约 + FileRunStore + RunJsonlWriter)。设计源 `ASYNC_RUN_DESIGN.md` 「双模 Harness」节;路线图 `2026-06-13-async-run-ROADMAP.md`。

## 目标

让 `DefaultHarness` 既能 sync(阻塞,行为=现状)又能 async(snapshot+fork+PH+后台 task)运行。开关由 `SessionConfig.async_mode` 持久化,`/async` 命令 toggle,ChatAgent 默认 async。**用户发新消息打断在跑的 async run → 当前 run 退后台继续跑**。Sync 模式行为与 Phase 2 完全一致。

## 接口改动(已审批)

1. `HarnessContext.async_mode: bool = False` —— `src/xana/harness_sdk/contracts.py`
2. `SessionConfig.async_mode: bool = True` —— `src/xana/session_config/contracts.py`(默认 True,聊天场景异步)
3. `/async` toggle 命令 —— `src/xana/agent_adapter/commands.py`(参考 `/thinking`/`/streaming`)
4. `MainWriter.enqueue_replace_placeholder(run_id, replace_with)` 具体方法 —— `src/xana/memory/main_writer.py`(**不进 ABC**)

**默认值链**:`SessionConfig.async_mode`(用户级,True) > `ChatAgent` 构造时覆盖(True) > `Harness 默认`(False)。子 agent 不覆盖,保持 sync。

**on→off 守卫**:切换时若 session 有 running PH → 拒绝,提示用户等后台 run 完成或 `/cancel`。

---

## 任务拆分(7 Task,Subagent-Driven)

每 Task 派 implementer + 2-stage review(spec / quality)。Task 间顺序依赖。

### Task 1 — `HarnessContext.async_mode` 字段

**文件**:`src/xana/harness_sdk/contracts.py`

加一行 `async_mode: bool = False`,放 `config_snapshot` 之前。

**测试**(`tests/test_harness_context_async_mode.py`):
- 默认值 = False
- 显式 True 可设置且 frozen 不可改

### Task 2 — `SessionConfig.async_mode` 字段 + `FileSessionConfigStore` round-trip

**文件**:
- `src/xana/session_config/contracts.py`:加 `async_mode: bool = True`
- `src/xana/session_config/file_store.py`(或同等实现):JSON 序列化包含新字段;读取旧 JSON 无字段时默认 True

**测试**(`tests/test_session_config_async_mode.py`):
- 默认值 True
- 写盘 + 读回保持
- 缺字段的旧 JSON 读回得 True

### Task 3 — `/async` toggle 命令 + `/streaming` `/thinking` 与之对齐

**文件**:`src/xana/agent_adapter/commands.py`

- 新增 `CommandSpec(name="/async", description="异步模式开关 on/off(无参数切换)")`
- `_cmd_async` 实现:无参 toggle,`on`/`off` 显式
- **on→off 守卫**:读 `ActiveContextStore.snapshot(session_id, agent_name)`,如有 `Placeholder` 且 `status="running"` → 返回 `CommandResult(ok=False, message="有后台 run 在跑,请等完成或 /cancel")`
- 守卫需要的 `session_id`/`agent_name`:`session_id` 从 `inbound` 拿;`agent_name` 取 session 当前 agent(从 `SessionStore.get(session_id).current_agent`,缺省 "chat")
- `_persist_with` 加 `async_mode` 形参

**测试**(`tests/test_command_async_toggle.py`):
- 无参 toggle 翻转
- `on`/`off` 显式生效
- 无效参数报错
- on→off 有 running PH 时拒绝,session 配置不变
- on→off 无 running PH 时成功

### Task 4 — ChatAgent 把 `SessionConfig.async_mode` 透到 `HarnessContext`

**文件**:`src/xana/agents/chat_agent.py`(`DefaultAgentRequestMapper.map`)

- 读 `cfg.async_mode`(cfg 已加载),默认 True(ChatAgent 覆盖 Harness 默认 False)
- 填入 `HarnessContext(async_mode=...)`
- `config_snapshot` 加 `"async_mode": ...` 便于日志/observability

**测试**(`tests/test_chat_agent_async_mode_wiring.py`):
- 无 session config 时 ctx.async_mode = True(ChatAgent 默认)
- session config async_mode=False 时 ctx.async_mode = False

### Task 5 — `MainWriter.enqueue_replace_placeholder` 方法

**文件**:`src/xana/memory/main_writer.py`

- `MainWriter` 加方法:
  ```python
  async def enqueue_replace_placeholder(
      self, *, session_id: str, agent_name: str, run_id: str, replace_with: Sequence[MemoryMessage]
  ) -> None:
      await self._run(lambda: self._inner_store.replace_placeholder(
          run_id=run_id, replace_with=tuple(replace_with)))
      for msg in replace_with:
          await self._run(lambda m=msg: self._inner_mirror.append(
              HistoryRow(... payload=m, session_id=session_id, agent_name=agent_name ...)))
  ```
- `_QueuedActiveContextStore.replace_placeholder` 已在 Phase 2 经过 queue,这里复用即可
- Mirror append 的 `row_id`/`kind`/`ts` 字段按 `MemoryMessage` 推断(role→kind 映射,沿用 `file_history` 现有约定)

**测试**(`tests/test_main_writer_replace_placeholder.py`):
- enqueue 后 inner_store 的 messages 中 PH 已被替换为 messages 序列
- mirror 收到逐条 append
- 多次 enqueue 串行无交错

### Task 6 — `DefaultHarness._invoke` 接 async 分支

**文件**:`src/xana/harness_sdk/default.py`

`_invoke` 入口检查 `ctx.async_mode`:

- **False(sync)**:走原路径(Phase 2 行为不变)
- **True(async)**:
  1. `snapshot = await ctx.active_context_store.snapshot(session_id, agent_name)` — 含 messages + 现有 PHs
  2. 追加用户消息 `await ctx.active_context_store.append_message(...)` (经 MainWriter)
  3. 新生成 `run_id = new_run_id()`,`label = user_input[:12]`
  4. `await ctx.active_context_store.append_placeholder(session_id, agent_name, Placeholder(run_id, label, status="running", jsonl_path=...))` (经 MainWriter)
  5. fork run context:`run_ctx_messages = [m for m in snapshot if isinstance(m, MemoryMessage)] + [new_user_msg]`(PH 不进 run ctx)
  6. `asyncio.create_task(self._run_background(run_id, run_ctx_messages, ctx))` — 不 await
  7. 立即向调用者 yield 一个 `HarnessEvent(kind="turn_started", run_id=run_id, payload={"async": True})` 然后**直接 return**(generator 关闭) — 用户消息已落 main,后台 run 自行走完
- `_run_background(run_id, msgs, ctx)`:
  - 拿 `RunJsonlWriter` (Phase 2 已 boot 注入,通过 `ctx.metadata["run_jsonl_writer"]` 取,或新增 `ctx` 字段 — 见下方决策)
  - 走原 `_invoke` query loop,事件流的 message 落 run JSONL(替代落 main)
  - 完成 → `run_jsonl_writer.finalize(run_id, "completed")` → `main_writer.enqueue_replace_placeholder(...)`
  - 异常 → `finalize(run_id, "failed")` → `enqueue_replace_placeholder` 写入 info 消息("此回答因 …  未完成。")

**决策点 — 后台 run 怎么拿 `RunJsonlWriter` 和 `MainWriter`**:
- 选 A:`HarnessContext` 加 `run_jsonl_writer` 和 `main_writer` 字段(再改 contracts)
- 选 B:通过 `ctx.metadata` 字典传(无 contracts 改动)
- **本 plan 选 B**(metadata 传),避免再次扩 contracts;boot 端在生成 `HarnessContext` 前把这两个对象塞进 metadata

**测试**(`tests/test_harness_async_invoke.py`):
- async_mode=True:invoke 立即返回 turn_started,后台跑完后 PH 被替换 + run JSONL 落盘+marker
- async_mode=False:行为与 Phase 2 完全一致(回归)
- 后台 run 异常:PH 替换为 info 消息

### Task 7 — boot wiring + 中断行为 + 守卫测试

**文件**:`src/xana/runtime/boot.py`

- `RunJsonlWriter` 已在 Phase 2 创建(`boot_result.run_jsonl_writer`)
- `ChatAgent` build 路径上:`HarnessContext` 生成时 `metadata={"run_jsonl_writer": ..., "main_writer": ...}`(由 mapper 注入,需要 mapper 持有这两个引用 — boot 注入到 mapper 构造参数)
- `DefaultAgentRequestMapper.__init__` 加 `run_jsonl_writer` / `main_writer` 形参,`map()` 写入 metadata

**用户打断行为**:不在 harness 层做,gateway 已经把新 inbound 直接送进新一轮 `_invoke` — async 下旧 run 的 task 仍在跑,新 invoke 拿新 snapshot 追加新 user + 新 PH,自然形成「旧 run 退后台」的语义。**Phase 3 不实现 Cancel(留 Phase 5)**,所以打断后旧 run 跑完仍替换其 PH。

**测试**(`tests/test_phase3_e2e.py`):
- E2E:async 模式下两条相继 user 消息 → main 含 [user1, PH1, user2, PH2] → 两 PH 都被替换
- `/async off` 在有 running PH 时被拒绝
- session 重启后 `async_mode` 配置保留

---

## 跨 Task 不变量

- Sync 路径 0 改动 → Phase 2 测试全绿(回归)
- 三层规矩:contracts/interfaces/implementation;新方法只放实现层(MainWriter),不进 ABC
- `boot()` 仍是唯一组合点
- 不引入 Cancel/Resume(Phase 5),不引入 tab UX(Phase 4),不改 compact(Phase 6)
- PH 在 ActiveContext 序列里通过 Phase 2 的 `snapshot()` 返回的混合 tuple 表达,`messages` 字段仍只含 `MemoryMessage`

## 已知 latent(不在 P3 解决)

- MainWriter consumer 异常 → 未决 future 死锁(多生产者时):P3 引入多生产者(后台 run task + 主 invoke + compact),**本 plan 必须修**:`_consume` 异常时把 `self._broken=True` 并把所有 pending future setexception。在 Task 5 一并处理。
- 后台 run 与 sync run 并存(`/async off` 切换瞬间):由 on→off 守卫挡住,不存在
- compact 丢 PH:Phase 6 处理

## 验收

- 704 个原测试 + 新增 Task 测试全绿
- 手测:`xanac` async on → 连发两条 → 第一条仍跑完替换 PH
- 飞书/tg 行为应当与现在一致(单条同步语义),`/async on` 后才出现后台 run
- 提交后 `xana sync`(serve 自动重启)
