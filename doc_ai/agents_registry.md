---
title: agents registry
type: doc
status: active
---

# agents registry

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/agents/interfaces.py) — `Agent` / `AgentRegistry` ABC
- [chat_agent.py](../src/xana/agents/chat_agent.py) — `ChatAgent` / `build_chat_agent_spec`
- [registry.py](../src/xana/agents/registry.py) — `FileAgentRegistry` / `agent_spec_from_doc`（`AgentRegistry` 首个实现）
- [boot.py](../src/xana/runtime/boot.py) — 实际组装与注册点

## 职责

定义 agent 的可调用形态（`Agent` ABC）和注册表合约（`AgentRegistry`）；`ChatAgent` 是 v1 唯一的 `Agent` 实现。

## Agent 接口语义

- `spec() -> AgentSpec`
- `send(request) -> AsyncIterator[AgentEvent]` — 一次用户输入的完整事件流。
- `control(request) -> AsyncIterator[AgentEvent]` — 控制操作（v1 `ChatAgent` 未实现，恒返回 `ok=False, reason="not implemented"`）。
- `cancel(session_id, run_id=None)` — 请求取消。

## ChatAgent

构造注入 `Harness` 与各 registry/store（同 [agents_request_mapping](agents_request_mapping.md) 的 mapper 依赖），内部持有 `DefaultAgentRequestMapper` + `DefaultAgentEventMapper`。

- `send`：mapper 翻译出 `HarnessRequest`/`HarnessContext` 后调 `harness.invoke`，逐事件映射后 yield；收到 `final/error/cancelled` 由调用方截断。
- `cancel` 语义分两级（P5 修复后）：
  - 带 `run_id` 的取消由 gateway 原地翻转该 run 的 `AbortSignal`，在跑 turn 的工具轮询点立即生效；`ChatAgent.cancel` 不额外记 pending（避免下一个用户 submit 在第一轮就被误 abort）。
  - 无 `run_id`（turn 间隙到达的取消）记入 `_pending_cancel_sessions`，下一次 `send` 时在新 context 的 abort 信号上 `trigger("user_cancel")`，实现 next-turn 语义。
- builtin spec：`build_chat_agent_spec()` 返回 name=`chat`、qualified_name=`builtin.chat`、prompt_id=`chat-default` 的 `AgentSpec`。

## AgentRegistry 与 v1 实际注册路径

`AgentRegistry`（`register` / `get` / `list_specs`）已有首个实现 `FileAgentRegistry`（[registry.py](../src/xana/agents/registry.py)）：从 `{root}/*/AGENT.md` 加载角色定义文档（共享 [MarkdownDocParser](../src/xana/skills/markdown_loader.py)，frontmatter `kind: agent`），经 `agent_spec_from_doc()` 映射成 [AgentSpec](agents_spec.md)，再由注入的 `agent_factory` 构造 `Agent` 实例（boot 给的是共享 harness/stores 的 `ChatAgent` 工厂）。坏文档收集为 `SkillManifestError` 返回、不中断扫描；root 不是目录时返回空。

实际组装在 `runtime/boot.py`：boot 创建 `DefaultAgentGateway(agents={}, router=DefaultAgentRequestRouter(default_agent="chat"))`，构造 `ChatAgent` 后 `gateway.agents["chat"] = chat_agent`（builtin chat 保持默认路由不变）；随后 `FileAgentRegistry(root={XANA_HOME}/agents, ...)` 加载文件 agent，把每个加载成功的 agent 注册进 `gateway.agents`（键为 spec 的 component name）。加载错误记 warning 跳过。registry 实例本身也放进 `BootResult.agent_registry` 供自省。

v1 的「registry 生效路径」仍是 gateway 上的 dict——`DefaultAgentRequestRouter` 默认路由到 chat agent，`/agent` 前缀等未来机制忽略文件 agent；文件 agent 要可被路由是后续工作。
