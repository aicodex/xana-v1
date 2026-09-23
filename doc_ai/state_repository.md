---
title: state repository
type: doc
status: active
---

# state repository

## 上级模块

- [state](state.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/state/interfaces.py) — `StateRepository`
- [in_memory.py](../src/xana/state/in_memory.py) — `InMemoryStateRepository`
- [boot.py](../src/xana/runtime/boot.py) — `_CompositeStateRepository`（boot.py:128）

## 职责

`StateRepository` 是 session/run 两个 store 的组合门面：`sessions() -> SessionStore`、`runs() -> RunStore`。它不定义新行为，只是把两类状态记录的取件口收拢成一个注入参数（ChatAgent 构造时拿的就是 `StateRepository`，不是单个 store）。

## 实现形态

- **`InMemoryStateRepository`**（in_memory.py）：两个内存 store 的组合，`history_root` 为空的 boot 路径使用（见 [runtime_boot](runtime_boot.md)）。
- **`_CompositeStateRepository`**（boot.py:128，boot 模块私有）：`sessions=InMemorySessionStore()` + `runs=FileRunStore(...)` 的组合。即当前持久化形态：**run 记录落盘（FileRunStore），session 记录仍只在内存**。boot 有 `history_root` 时使用。

两种形态暴露的都是同一对 `SessionStore` / `RunStore` ABC——阶段二起 `RunStore` ABC 含 `list_all(status=...)`（任务面板 + shutdown 遗留扫描的消费口），两个实现（FileRunStore / InMemoryRunStore）均已实现。

## 不变量

- 门面只聚合、不新增状态语义；所有读写语义在 `SessionStore`/`RunStore` 各自的实现里（见 [state_session](state_session.md)、[state_run](state_run.md)）。
- 选择哪个组合形态的唯一开关是 `boot(history_root=...)`，调用方不能绕过 boot 自建持久化组合。

## 验收要求

- 输入/输出与 [state](./state.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
