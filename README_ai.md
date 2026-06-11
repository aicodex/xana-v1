---
title: xana Memory Index
type: goal
status: active
---

# xana Memory Index

> 根文档只维护目标、模块划分和入口链接。
> 系统级文档是 3 层语义：根索引 → 模块设计 → 模块详细设计。
> 文件结构保持扁平：`doc_ai/module.md`、`doc_ai/module_topic.md`，便于整体复制到 `.xana` 运行态目录。

## 项目入口

- **模块设计总览**：[doc_ai](doc_ai/README_ai.md) — 模块边界和文档入口
- **系统设计**：[system](doc_ai/system.md) — 全局数据流、状态权威、恢复流程
- **经验入口**：[exp_ai](exp_ai/README_ai.md) — 经验、迁移、复盘，非目标架构权威
- **TODO 入口**：[todo_ai](todo_ai/README_ai.md) — 特殊记忆树，维护待办和执行 scratch

## 核心模块

- [config](doc_ai/config.md) — 配置加载、系统/项目/agent 覆盖规则。
- [runtime](doc_ai/runtime.md) — 进程生命周期、启动顺序、调度外壳。
- [harness_sdk](doc_ai/harness_sdk.md) — turn loop、暂停/恢复/取消、事件流和 usage 快照。
- [agents](doc_ai/agents.md) — agent spec、outer API、会话侧行为包装。
- [tools](doc_ai/tools.md) — 工具校验、权限、沙箱、执行边界和输出 schema。
- [skills](doc_ai/skills.md) — 技能元数据、激活规则、上下文/提示注入。
- [agent_adapter](doc_ai/agent_adapter.md) — CLI/TG/Feishu/HTTP 等外部协议翻译。
- [llm_driver](doc_ai/llm_driver.md) — 模型请求、流式响应、tool call 解析、usage 提取。
- [memory](doc_ai/memory.md) — active context 权威、history mirror、treemem/search/injection。
- [observability](doc_ai/observability.md) — health、trace、debug snapshot、诊断视图。

## 支撑模块

- [component](doc_ai/component.md) — 共享组件元数据、加载、校验、override、registry 合约。
- [hooks](doc_ai/hooks.md) — 生命周期 hook 合约。
- [policy](doc_ai/policy.md) — 权限、沙箱、approval 策略合约。
- [state](doc_ai/state.md) — session、run、checkpoint、resume、usage 状态合约。

## 合约入口

- [module_io_map](doc_ai/module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](doc_ai/replacement_rule.md) — 实现可替换的硬条件
- [io_contract_template](doc_ai/io_contract_template.md) — 新模块合约模板

## 核心规则

1. Define inputs and outputs before implementation.
2. Runtime state has one authority per concern.
3. Persistent logs are mirrors unless a module contract explicitly says otherwise.
4. Adapters translate protocols; they do not own agent logic.
5. Agents configure harness behavior; they do not own the turn loop.
6. Tools and skills are lifecycle-managed components, not filename conventions.
7. Config comes from system/project/agent config layers, never hardcoded prompts.

## 安装态规则

- `README_ai.md`、`doc_ai/`、`exp_ai/`、`todo_ai/` 必须可以整体复制到 `.xana/`。
- 运行状态修改 mem 时只改 `.xana` 里的副本，不改源码树。
- 文档链接全部使用相对路径，不能依赖源码绝对路径。
