---
title: agents registry
type: doc
status: active
---

# agents registry

## 上级模块

- [agents](agents.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/agents/interfaces.py) — `Agent` / `AgentRegistry` ABC（`register` / `get` / `list_specs` / `refresh` / `remove`）
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
- builtin spec：`build_chat_agent_spec(defaults)` 返回 name=`chat`、qualified_name=`builtin.chat`、prompt_id=`chat-default` 的 `AgentSpec`；`tools` / `disallowed_tools` / `skills` 取项目 `agent_defaults`（见 [agents_spec](agents_spec.md)）。

## AgentRegistry 与实际注册路径

`AgentRegistry`（`register` / `get` / `list_specs` / `refresh(force=False)` / `remove(name)`）的实现是 `FileAgentRegistry`（[registry.py](../src/xana/agents/registry.py)，设计见 [agent-delegation-design](../docs/design/agent-delegation-design.md) §7）：

- **两级目录**：构造参数 `roots` 按优先级从低到高——`("user", {XANA_HOME}/agents)`、`("project", <project_root>/.xana/agents)`；扫描各 root 的 `*/AGENT.md`（共享 [MarkdownDocParser](../src/xana/skills/markdown_loader.py)，frontmatter `kind: agent`），同名时后者覆盖前者。`reserved`（内置 agent 名，boot 传 `BUILTIN_AGENT_NAMES`）不能被文件覆盖，同名文件跳过并报错。root 不是目录时跳过。
- **直接维护共用 agent 表**：`table` 是 gateway 共用的那份 `agents` dict（不是 boot 一次性拷贝）。`refresh()` 按每个文件的 (路径, (mtime_ns, size)) 与已加载状态比对：新增 / 变更的经 `agent_spec_from_doc()` 映射成 [AgentSpec](agents_spec.md)，再由注入的 `agent_factory` 构造 `Agent` 实例（boot 给的是共享 harness/stores 的 `ChatAgent` 工厂）写进表；文件已不存在的从表中移除。最多每 `min_interval_s=2.0` 秒真正扫描一次，`force=True` 立即扫描；`load()` = `refresh(force=True)`（boot 调用）。
- **坏文档**：收集为 `SkillManifestError` 返回并记 warning（每个 (路径, 版本) 只报一次），不中断扫描；此前加载成功过的文件改坏时保留上一个可用版本，不静默删掉正在用的 agent。
- **项目默认**：`defaults`（`AgentDefaults`，boot 传 `system_cfg.agent_defaults`）用于没写 `tools` / `skills` 的 AGENT.md。
- **删除**：`remove(name)` 强制刷新后删掉该 agent 当前生效的 AGENT.md（目录空了一并删），再刷新；内置或不存在的 agent 抛 `LookupError`。删掉项目级同名 agent 后，用户级同名 agent（若有）随即生效。
- `get` / `list_specs` 只返回由本注册表维护的文件 agent（不含内置）。

调用刷新的时刻：gateway 路由消息前（节流）、`agent_run`、可委派 agent 列表（节流）；`/agent` 与 `agent_define` 用 `force=True`。不开后台监听线程。

实际组装在 `runtime/boot.py`：boot 先建空的 `agents` 表与 `FileAgentRegistry(roots=..., table=agents, reserved=..., defaults=...)`，构造 `ChatAgent` 后 `gateway.agents["chat"] = chat_agent`（builtin chat 保持默认路由），再 `load()` 文件 agent。registry 实例注入 gateway（路由前刷新；目标 agent 不存在时回复「agent X 不存在(可能已删除),用 /agent 选择其他 agent」）、`DefaultCommandRouter`（`/agent`）、`agent_run`、`agent_define`，并放进 `BootResult.agent_registry`。

路由：`DefaultAgentRequestRouter` 默认路由到 chat；会话绑定的 agent（`SessionConfig.agent`，`/agent <名字>` 设置）决定 `target_agent`，文件 agent 与内置 agent 同样可被路由。
