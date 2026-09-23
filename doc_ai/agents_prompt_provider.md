---
title: agents prompt provider
type: doc
status: active
---

# agents prompt provider

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/agents/interfaces.py) — `PromptProvider` ABC
- [system_prompt.py](../src/xana/harness_sdk/system_prompt.py) — v1 实际系统提示拼装（13 段）

## 职责

`PromptProvider` 定义 agent prompt 三个组成部分的取数合约：identity（身份段）、instructions（任务指令段）、memory_block（记忆段）。

## 接口语义

- `identity(spec: AgentSpec) -> str` — 由 spec 生成身份段文本。
- `instructions(request: AgentRequest) -> str` — 由本次请求生成指令段。
- `memory_block(session_id: str) -> str` — 取该会话的记忆注入段。

## 当前状态

仅接口合约，无实现：v1 没有任何 `PromptProvider` 实现类，三个方法均未被调用。

v1 的实际 prompt 路径是 harness 静态拼装：`build_system_prompt` 按固定 13 段顺序输出，身份/覆盖来自 `AgentSpec.identity` / `AgentSpec.system_prompt`（经 [DefaultAgentRequestMapper](agents_request_mapping.md) 写入 `config_snapshot`），记忆注入由 harness 通过 `MemoryInjectorRegistry` 以动态段形式追加。`AgentSpec.prompt_id` 目前只是标识，没有模板查表机制。

后续若要实现 `PromptProvider`，需要先把 harness 的静态拼装点参数化，再落实现与测试。
