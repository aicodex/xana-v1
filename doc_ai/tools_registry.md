---
title: tools registry
type: doc
status: active
---

# tools registry

## 上级模块

- [tools](tools.md) — 模块设计

## 代码锚点

- [interfaces.py](../src/xana/tools/interfaces.py) — `ToolRegistry`
- [registry.py](../src/xana/tools/registry.py) — `InMemoryToolRegistry`
- [boot.py](../src/xana/runtime/boot.py) — 唯一注册点（17 个内置工具全部在此 register）

## 职责

工具注册与按名查找。key 是 `tool.spec().component.name`。

## InMemoryToolRegistry

- `register(tool)`：以 `ComponentSpec.name` 为键存入 dict，同名覆盖。
- `unregister(name)`：pop 并返回被移除的 `Tool`，未知名返回 `None`（MCP 连接失效/热更新时摘除工具用；`FileToolRegistry` 只注销 programmatic 工具，绝不删 TOOL.md，见 [mcp](mcp.md)）。
- `get(name)`：按名取工具，miss 返回 `None`。
- `list_specs()`：返回全部 `ToolSpec` 元组（`tool_search` 用它扫 deferred 工具，SkillsListing 之外的所有 schema 拼装也依赖它）。MCP 发布的工具带 `toolset="mcp"`（`BUILTIN_TOOLSETS` 中 default=True）、`defer` 继承 server 配置。

进程内、非持久化；生命周期随 boot。按 AGENTS.md 规则 4，`boot()` 是唯一组合点，`register()` 只在那里调用（测试除外）。

## 查找优先级

`DefaultHarness._dispatch_tool` 的查找顺序：

1. extension 注入的工具表（`ext_tools`）——extension 工具优先，且永远全量 schema、可覆盖同名内置工具，不参与 defer。
2. 共享 `ToolRegistry`（boot 注册的内置工具都在这；MCP 经审核发布的工具也在这，MCP 不覆盖内置/文件/其他 server 同名工具，冲突者隔离）。
3. 都 miss → `failure_kind="not_found"` 结果。

`ToolSearchTool` 构造时持有 registry 引用（构造注入同一个 `InMemoryToolRegistry` 实例），用于枚举 `defer=True` 的 spec。

## MCP 工具的生命周期归属

MCP 工具注册/注销由 `mcp` 模块的连接状态机驱动（失效摘除、恢复重审后重新发布），但一次调用的完整生命周期仍走本文同一条链：`check_permission` 不额外开后门（manual/yolo/auto、plan mode 与会话批准照常生效），传输失败返回 `unsupported + retryable` 且不自动重放，旧 generation 工具在 `execute` 前置检查即拒绝。详见 [mcp](mcp.md)。
