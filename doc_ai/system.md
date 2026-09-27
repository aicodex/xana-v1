---
title: System Design
type: doc
status: active
---

# System Design

## 相关入口

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [runtime](runtime.md) — 启动和生命周期
- [harness_sdk](harness_sdk.md) — turn lifecycle
- [memory](memory.md) — active context 和 history mirror
- [agent_adapter](agent_adapter.md) — 外部协议入口

## Core Idea

The system is organized around explicit contracts. Each module has:
- input types
- output types
- lifecycle hooks
- ownership boundaries
- forbidden dependencies

## State Authorities

- Active runtime context: `memory.ActiveContextStore`.
- Durable conversation mirror: `memory.HistoryMirror`.
- Turn lifecycle: `state.RunStore`（`RunRecord.status`），由 `DefaultHarness` 驱动转换。
- Model choice and effort: `config` plus session state, not adapter state.
- External protocol state: `agent_adapter`, only for transport concerns.
- 路径：`config/paths.py` 是唯一解析点（`xana_home()` / `project_id()` / `project_home()` / `project_root()`）。

## 数据布局（项目）

一个项目一个 serve 进程（`xana serve --project <id>` 设置 `XANA_PROJECT`，缺省 `default`），详见 [project-design](../docs/design/project-design.md)：

- 用户级 `~/.xana/`（`$XANA_HOME`）：`config/system.json`（模型、搜索、策略等）、`credentials/`、用户级 `agents/` `skills/`、`code/` `bin/` `mem/`。
- 项目私有 `~/.xana/projects/<id>/`：`project.json`（`{"root": 项目目录}`）、`config.json`（channels / daemon / sessions / tools / agent_defaults）、`sessions/` `runs/` `logs/` `run/`（锁 / pid / socket）、`channels/`（配对）、`scheduled_tasks.json`、`evolution/`、`worktrees/`。
- 项目目录（default = `~/.xana/projects/default/workspace`）：`README_ai.md` 记忆树 + 项目级 `.xana/agents/`。

## Normal Message Flow

`Adapter -> Agent outer API -> Harness -> LLM Driver -> Tool/Skill dispatch -> Harness events -> Agent -> Adapter render`

## Recovery Flow

`Config -> HistoryMirror replay -> ActiveContextStore -> startup_scan（run 仲裁）-> Adapters start`

History is only a mirror unless the memory contract is explicitly changed.
