---
title: cli
type: doc
status: active
---

# cli

## 定位

`cli` 是用户直接面对的进程入口层，只做两件事：机器管理命令的分发（`xana`），和到 serve daemon 的交互客户端（`xanac`）。它不实现 agent / harness / adapter 的任何业务逻辑，只是组装和转发。

## 入口

- `xana`（`src/xana/cli/xana.py`）— argparse 顶层分发器，子命令 `setup` / `sync` / `serve` / `pair` / `project` / `login` / `logout`，全部是"机器管理"性质的非交互或一次性命令。`serve --project <id>` 以某个项目运行（缺省 `default`，一个项目一个 serve，见 [project-design](../docs/design/project-design.md)）。
- `xanac`（`src/xana/cli/xanac.py`）— 交互聊天客户端，HTTP+SSE 瘦客户端，连本机某个项目的 `xana serve` daemon（`--project <id>`，缺省 default）；不是独立 runtime。
- `xana project create <id> [--root <目录>] [--port N]` / `xana project list`（`src/xana/cli/commands/project.py`）：注册新项目（缺省目录 `~/.xana/projects/<id>/workspace`，端口缺省自动分配空闲端口）/ 列出项目、运行状态、端口、飞书机器人、目录；实际逻辑在 `runtime/projects.py`（与聊天里的 `project_manage` 共用）。同文件的 `ensure_default_project()` 创建默认项目并一次性迁移旧布局（由 `xana sync` 调用，见 [cli_setup](cli_setup.md)）。

`xana setup` 会往 `~/.xana/bin/` 写两个自包含 launcher（`xana` / `xanac`），launcher 运行时把 `sys.path` 指向 `~/.xana/code/`（即最近一次 `xana sync` 的产物），因此 launcher 不依赖源码仓库位置，sync 升级后自动生效。

## 详细设计

- [cli_setup](cli_setup.md) — `xana setup` 首装流程 + setup wizard 模型/渠道配置 + `xana sync` 的 code/mem 同步、默认项目创建 / 旧布局迁移、各项目 serve 重启
- [cli_login](cli_login.md) — `xana login` / `xana logout`：Kimi OAuth device flow、token 存储、`/models` 模型登记
- [cli_serve](cli_serve.md) — `xana serve [--project <id>]`：项目解析、adapter 组装、daemon HTTP+SSE 启动、每项目单实例锁、pidfile、优雅退出
- [cli_xanac](cli_xanac.md) — `xanac`：endpoint 解析、serve 自动拉起、one-shot 与 Textual TUI

## 全局合约

- [module_io_map](module_io_map.md) — 模块输入/输出/所有权总表
- [replacement_rule](replacement_rule.md) — 替换规则

## Owns

- `xana` / `xanac` 命令行入口、子命令分发、launcher 安装、PATH 配置。
- 首装引导（seed `system.json`、sync、默认项目与旧布局迁移、wizard）、项目注册与列表、渠道用户配对管理、Kimi 登录。
- serve 进程的拉起入口与 pidfile 生命周期。
- xanac 的 daemon HTTP+SSE 客户端（含自动拉起 serve）。

## Does Not Own

- agent 行为、harness turn loop、memory、tools、policy（属于 [harness_sdk](harness_sdk.md) / [agents](agents.md) / 各支撑模块）。
- 渠道协议翻译与卡片渲染（属于 [agent_adapter](agent_adapter.md)；cli 只做进程级组装和 xanac 侧的 card 消费）。
- 模型 provider 请求（属于 [llm_driver](llm_driver.md)；login 只调它的 `kimi_oauth` 存取 token）。
- runtime 组合逻辑（属于 [runtime](runtime.md)；serve 只是 `boot()` 的一个调用方）。

## Inputs

- 命令行参数与 stdin/TTY 状态
- 用户级 `~/.xana/config/system.json`（models 等）+ 项目级 `~/.xana/projects/<id>/config.json`（channels / daemon / sessions 等），经 `project_config.load_system_config()` 合并读取
- 环境变量（`XANA_HOME`、`XANA_PROJECT`（`--project` 设置）、`XANA_KIMI_REGION`、`FEISHU_*`、`TELEGRAM_*`）
- serve daemon 的 HTTP+SSE 端点

## Outputs

- `~/.xana/` 目录树：用户级 `config/system.json`、`code/`、`mem/`、`bin/` launcher、`credentials/kimi.json`；项目级 `projects/<id>/`：`project.json`、`config.json`、`channels/pairings.json`、`run/serve.lock` / `run/serve.pid` / `run/serve.sock`、`logs/serve.log`、`workspace/`（default 项目的项目目录）
- 用户 shell rc 里的 PATH marker block
- stdout 上的命令结果与 xanac 卡片渲染
- 给 gateway 的 `InboundMessage`（经 daemon `/submit`，与渠道 adapter 同一条入站路径）
