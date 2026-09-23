---
title: memory history mirror
type: doc
status: active
---

# memory history mirror

## 上级模块

- [memory](memory.md) — memory 模块设计
- [memory_active_context](memory_active_context.md) — active context 权威

## 代码锚点

- [contracts.py](../src/xana/memory/contracts.py) — `HistoryRow`, `ReplayResult`, `Placeholder`
- [interfaces.py](../src/xana/memory/interfaces.py) — `HistoryMirror`, `MemoryReplayer`（placeholder 三操作 :80-98）
- [active_context.py](../src/xana/memory/active_context.py) — `InMemoryHistoryMirror`
- [file_history.py](../src/xana/memory/file_history.py) — `FileHistoryMirror` / `FileMemoryReplayer` / 中断检测
- [runs_loader.py](../src/xana/memory/runs_loader.py) — per-run JSONL → `MemoryMessage`
- [main_writer.py](../src/xana/memory/main_writer.py) — `MainWriter` 单写者队列
- [write_queue.py](../src/xana/memory/write_queue.py) — `WriteQueue` / `WriteQueueConsumer`

## 职责

`HistoryMirror` is a durable append mirror of context messages plus metadata.

It owns:
- append-only rows
- replay source material for cold start
- minimal metadata needed to rebuild active context

It does not own:
- live context authority
- current model-visible memory
- retry semantics for unfinished turns

## 接口语义

- `append(row)` writes a history row after active context is updated or as part of the same transaction boundary.
- `replay()` scans durable rows and reports loaded row/session counts.
- `MemoryReplayer.rebuild_active_context(...)` reconstructs `ActiveContextStore` from mirror rows.

### Placeholder 三操作（HistoryMirror 侧，interfaces.py:80-98）

`HistoryMirror` 也声明了 `append_placeholder` / `replace_placeholder` / `replace_range`，语义与 store 侧对应（占位符落盘、按 run_id 回填、compact 区间折叠）。**当前所有实现都是 `NotImplementedError`**：

- `InMemoryHistoryMirror`：三个操作均 `raise NotImplementedError("Group B/C")` / `("Group H")`（active_context.py:402-420）。
- `FileHistoryMirror`：同样三个 `NotImplementedError`（file_history.py:88-106）。

因此 history 侧只有 `append(row)` / `replay()` / `read_rows()` 是活路径；占位符与 compact 写回只作用于 active context（store 侧 + SDK），mirror 不参与。`WriteQueueConsumer` 对 mirror 调用这两个操作时捕获 `NotImplementedError` 静默跳过（write_queue.py:91-113），所以队列 consumer 与未实现镜像可以安全共存。

## 文件后端（file_history.py）

boot 传 `history_root` 时启用（`boot(history_root=...)`，见 [runtime_boot](runtime_boot.md)）；不传则退回 `InMemoryHistoryMirror`。

- **`FileHistoryMirror`**：每 session 一个 `{root}/{session_id}.jsonl`，每行一条 JSON 序列化的 `HistoryRow`（`_row_to_json`），`append()` 纯追加（`open("a")`），跨进程重启不丢。
- **损坏行容忍**：`read_rows()` 逐行 `try/except`，解析失败的行记 warning 后跳过，不阻断恢复（file_history.py:75-80）。
- **`FileMemoryReplayer.rebuild_active_context(mirror, store)`**：依赖 mirror 暴露的 `read_rows()`；按 `(session_id, agent_name)` 分组，每组 `store.set()` 一个重建的 `ActiveContext`。没有 `read_rows` 方法的 mirror 视为无可恢复内容，返回零计数。
- **中断检测（CC 风格）**：`replay_and_detect_interrupts()` 先 replay 重建，再对每个 session 检查尾部——最后一条是 user/tool 消息（上轮未收尾）时追加一条 `MemoryMessage(role="user", content="Continue from where you left off.")`，让模型接续。
- **`runs_loader.runs_jsonl_to_memory_messages(path)`**：读 `runs/{run_id}.jsonl`（由 `state.file_runs.RunJsonlWriter` 产出），跳过 `{"marker": "run_completed"}` 终止行，容忍空行与损坏行，按序返回 `MemoryMessage` 元组——placeholder 回填时 run 真实消息的来源。

## 写入串行化：MainWriter 与 WriteQueue

多个并发 run 完成顺序任意，main context 写入必须串行，否则占位符回填会竞争消息列表。

- **`MainWriter`（main_writer.py:170）**：单一 consumer 循环 + 对 store/mirror 的透明包装。构造时产出 `writer.store` / `writer.mirror`（`_QueuedActiveContextStore` / `_QueuedHistoryMirror`），harness 拿到的就是这两个包装——所有写操作（set/append_message/compact/clear/append_placeholder/replace_placeholder/replace_range/mirror.append）被包成 `_WriteOp` 进同一个 `asyncio.Queue`，由 `start()` 拉起的独立 task 串行执行；读操作（get/snapshot/replay/read_rows）直透内层不排队。Phase 2 sync 模式下等价直调，代码路径为 Phase 3 后台 run 保留。consumer 崩溃时标记 `_broken` 并 drain 队列，挂起调用方 future 抛 `RuntimeError` 而不是挂死。`enqueue_replace_placeholder()` 把「store 回填 + 每条消息一次 mirror.append（带 run_id metadata）」作为有序序列入队。harness 关停时 `aclose()` 发 sentinel 停 consumer。不抽 ABC——boot 组装的 concrete helper。
- **`WriteQueue` + `WriteQueueConsumer`（write_queue.py:48）**：更窄的第二种队列，只装两种 item——`ReplaceItem`（run finalize：按 run_id 回填 PH）与 `ReplaceRangeItem`（compact finalize：把 `[start_idx, end_idx)` 折叠成 summary）。单一 consumer 独自 `await` 队列并应用到 store + mirror。**consumer 必须跑在独立的 asyncio task**（boot 用 `asyncio.create_task` 拉起），否则 sync 模式下 run consumer → `_invoke` → enqueue 自己等自己，死锁。

boot 里两条链都接线：`history_root` 存在时 store/mirror 经 `MainWriter` 包装，`WriteQueue` + consumer task 恒建（boot.py:190-216）。

## 不变量

- History is not the gold standard during normal runtime.
- History is a mirror and recovery input.
- If active context and history conflict during live runtime, active context wins.
- Mirror 的 placeholder/replace_range 操作当前未实现（`NotImplementedError`）；它们只是接口预留，活路径全在 store 侧。
