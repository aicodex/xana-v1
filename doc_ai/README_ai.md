---
title: doc_ai
type: doc
status: active
---

# doc_ai

`doc_ai` contains target-state architecture. It is the stable source of truth for what the system is supposed to be.

## 系统入口

- [system](system.md) — 全局数据流、状态权威、恢复流程
- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 实现替换规则
- [io_contract_template](io_contract_template.md) — 新合约模板

## 核心模块

- [config](config.md) — 配置加载、系统/项目/agent 覆盖规则。
- [runtime](runtime.md) — 进程生命周期、启动顺序、调度外壳。
- [harness_sdk](harness_sdk.md) — turn loop、暂停/恢复/取消、事件流和 usage 快照。
- [agents](agents.md) — agent spec、outer API、会话侧行为包装。
- [tools](tools.md) — 工具校验、权限、沙箱、执行边界和输出 schema。
- [skills](skills.md) — 技能元数据、激活规则、上下文/提示注入。
- [agent_adapter](agent_adapter.md) — CLI/TG/Feishu/HTTP 等外部协议翻译。
- [llm_driver](llm_driver.md) — 模型请求、流式响应、tool call 解析、usage 提取。
- [memory](memory.md) — active context 权威、history mirror、treemem/search/injection。
- [observability](observability.md) — health、trace、debug snapshot、诊断视图。

## 支撑模块

- [component](component.md) — 共享组件元数据、加载、校验、override、registry 合约。
- [hooks](hooks.md) — 生命周期 hook 合约。
- [policy](policy.md) — 权限、沙箱、approval 策略合约。
- [state](state.md) — session、run、checkpoint、resume、usage 状态合约。

## 命名规则

- `module.md`：模块设计，说明职责、输入输出、边界。
- `module_topic.md`：细节设计，按子模块/类职责拆分，例如 `memory_tree.md`、`memory_search_engine.md`。
- `exp_ai/*.md`：经验和迁移记录，可以挂到某个模块或 detail 上。

## TODO 关系

- [todo_ai](../todo_ai/README_ai.md) — 特殊记忆树，只维护待办；设计变更必须先更新 `doc_ai`。
