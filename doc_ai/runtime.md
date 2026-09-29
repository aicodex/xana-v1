---
title: runtime
type: doc
status: active
---

# runtime

## 定位

进程生命周期、`boot()` 组装、daemon HTTP+SSE、定时任务调度外壳；项目管理（`runtime/projects.py`）。

一个 serve 进程 = 一个项目（`XANA_PROJECT`，缺省 default）：boot 内所有项目私有路径经 `config/paths.project_home()` 取 `~/.xana/projects/<id>/`，用户级路径经 `xana_home()`（见 [project-design](../docs/design/project-design.md)）。

## 详细设计

- [runtime_boot](runtime_boot.md) — `boot()` 唯一组合点：memory 底座 → config → registry → gateway，`BootResult` 全量出口
- [runtime_lifecycle](runtime_lifecycle.md) — `xana serve` 启动/运行/关闭序列，daemon HTTP（/healthz、token 鉴权、SSE）
- [runtime_scheduler](runtime_scheduler.md) — `SchedulerService`/`FileSchedulerService` + `SchedulerRunner` + cron 工具 + schedule skill 的定时任务闭环

## 项目管理（runtime/projects.py）

命令行 `xana project` 与聊天里的 `project_manage` 工具共用，只管注册、查询、配置与启停，不涉及 serve 内部：

- `create_project(id, root=None, port=None, agent_defaults=None)`：root 缺省 `projects/<id>/workspace`；端口缺省 `free_port(17778)` 分配（跳过其他项目已用端口与被占端口），同时生成 daemon token；最后写 `project.json`（它存在即项目就绪），项目目录缺 `README_ai.md` 时补一个。已存在抛 `FileExistsError`，id 不合法抛 `ValueError`。
- `write_instructions(root, text)`：项目根目录还没有 `AGENTS.md`（项目说明，harness 每个会话读取）时写入，已有不覆盖（返回 False）。`ensure_default_project` 创建 default 项目时写入 `DEFAULT_INSTRUCTIONS`；`project_manage create` 的 `instructions` 经它写入。
- `project_info(id)` / `all_projects()` → `ProjectInfo(id, root, port, running_pid, feishu_bot, agent_defaults)`。
- `set_agent_defaults(id, …)`：改项目 config.json 的 `agent_defaults`（运行中的项目需重启生效）。
- `set_feishu_bot(id, creds)`：写入扫码注册得到的飞书机器人到该项目 `channels.feishu`，并把确认人 `open_id` 加入该项目 `channels/pairings.json` 白名单。
- `start_project(id)`：后台 `xana serve --project <id>`（日志 `projects/<id>/logs/serve.log`，新会话组）；`stop_project(id)`：SIGTERM，超时 SIGKILL。`running_pid(id)` 读 `run/serve.pid` 并判活——先 `waitpid(WNOHANG)` 回收本进程启动的已退出子进程（否则僵尸进程仍能通过 `kill(0)`）。

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

Process lifecycle, boot order, shutdown, scheduler shell, and top-level dependency wiring.

## Does Not Own

Agent behavior, tool execution logic, LLM provider logic, memory semantics, or adapter protocol details.

## Inputs

- `project_root`（serve 从 `projects/<id>/project.json` 解析）
- `DefaultConfigProvider`（可注入）
- `extension_registry`（可空）
- `history_root`（持久化开关，可空；serve 传 `projects/<id>/sessions`）

## Outputs

- `BootResult`（gateway、全部 registry、state、writer、scheduler 等组装产物）
- daemon HTTP app（`build_daemon_app`）
- runtime health（`/healthz`）
- 定时任务触发（到点 prompt 注回 session）
