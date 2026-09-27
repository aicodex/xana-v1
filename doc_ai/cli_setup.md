---
title: cli_setup
type: doc
status: active
---

# cli_setup

`xana setup` 与 `xana sync` 的安装期机制。实现在 `src/xana/cli/commands/setup.py`、`setup_wizard.py`、`sync.py`；默认项目创建与旧布局迁移在 `project.py` 的 `ensure_default_project()`（数据布局见 [project-design](../docs/design/project-design.md) §3、§6）。

## xana setup

一条命令完成新机引导，顺序固定：

1. **seed 配置**：`ensure_user_system_json()` 确保 `~/.xana/config/system.json` 存在（api key 只进这里，不进项目仓库）。
2. **跑 sync**：直接内联调用 `sync_cmd.run()`（见下文），物化 `~/.xana/code/` + `~/.xana/mem/`，并经 `ensure_default_project()` 创建默认项目 `default`（`~/.xana/projects/default/`：`project.json`、`config.json`、`workspace/README_ai.md`），旧布局就地迁移。
3. **写 launcher**：`~/.xana/bin/xana` 与 `~/.xana/bin/xanac`，自包含 Python 脚本——运行时解析 `$XANA_HOME`（默认 `~/.xana`），把 `~/.xana/code` 插入 `sys.path` 后调 `xana.cli.<cmd>.main()`。内容不变则保留（kept），变了才重写。
4. **配 PATH**：向 `~/.zshrc` 或 `~/.bashrc`（按 `$SHELL`）追加 `export PATH="<bin>:$PATH"`，包在 `# >>> xana bin path >>>` / `# <<< xana bin path <<<` marker block 里，重跑幂等；未知 shell 只打印提示。
5. **wizard**（可选）：`--no-wizard` 或 stdin 非 TTY 时跳过，否则进入 `run_wizard()`。

## setup wizard

交互式配置当前项目（`XANA_PROJECT`，缺省 default）与用户级模型，全部问答经过注入的 `ask` callable（测试用 canned answers 驱动）。重跑 `xana setup` 必然进入两个 step，但已配置段在第一个 prompt 回车即跳过、不清空现有配置。

**模型 step**：

- 首次（`models[]` 为空）：type / 名称 / api key 必填，base_url 可选；写入 `models[]` 后，若 `default_model.chat` / `.agent` 为空则填为新模型。
- 重配（已有模型）：第一个 prompt 回车直接跳过；否则按 name 同则替换、异则追加，并询问是否设为默认 chat/agent。
- 提示 Kimi 订阅用户走 `xana login`（OAuth），无需在此填 key。

**渠道 step**（三选一）：

1. 飞书：`register_feishu()`（[agent_adapter](agent_adapter.md) 的 `feishu_onboard`）扫码自动创建应用，把 `app_id` / `app_secret_key` / `domain` / `open_id` / `bot_name` / `bot_open_id` 写入当前项目 config.json 的 `channels.feishu`；所有用户首次发消息都需 `xana pair approve feishu <open_id>`。
2. Telegram：录入 `@BotFather` 的 bot token（形如 `123456:ABC-...`），可选逗号分隔的 `allowed_user_ids`（留空走配对审批）。
3. 跳过。

**落盘**：`_load_raw()` 读用户级 system.json 与当前项目 config.json 的合并 dict；`_save_raw()` 先 `SystemConfig(**data)` 校验再拆开写回——`channels` 经 `update_project_config` 写项目 config.json，其余非 `PROJECT_KEYS` 键原子写用户级 system.json（tmp + `os.replace`，chmod 0600）。wizard 结束后 `ensure_daemon_token(项目 config.json)` 保证项目的 `daemon.token` 存在（32 字节 hex，幂等保留旧值），并打印该项目的 unix socket（`daemon_socket()`）/ TCP 端口 / token 位置提示。

在聊天里给其他项目接飞书机器人走 default 项目的 `project_manage bind_feishu`（见 [tools](tools.md)），确认人自动加入该项目配对白名单。

## xana sync

把运行时代码和记忆物化到 `~/.xana/`，是 launcher 体系的地基：`~/.xana/bin/` 里所有 launcher 加载的都是 `~/.xana/code/`，运行的是**最后一次 sync 写入**的代码，与开发者的 live `src/` 检出无关。

**code 源解析顺序**：

1. `--source`（repo root 或 xana package dir，自动向下找含 `__init__.py` 的目录）；
2. `~/.xana/.sync_source` sidecar — 上次成功 sync 记录的路径。launcher 场景必需，因为此时 `xana.__file__` 指向的是 target 自己；
3. `Path(xana.__file__).parent`（首次 pip 安装时可用）。

**行为是 wipe + copy 而非 merge**，改名/删除的文件不会以陈旧形态残留。解析出的 source 若落在 target 树内则拒绝执行（自删除防护）。每次成功后把 source 写回 `.sync_source` sidecar。

**mem 同步**：从 code source 向上找到含 `pyproject.toml` 的 repo root，把顶层 `README_ai.md` 和 `doc_ai/`、`exp_ai/`、`todo_ai/` 复制到 `~/.xana/mem/`（同样 wipe + copy，忽略 `__pycache__` / `*.pyc` 等）。找不到 repo root（wheel 安装）则跳过。

**serve 停止 → 迁移 → 重启**（`--no-restart` 可关）：

1. **同步前**停掉所有在运行的项目 serve：`serve_pid_paths()` 收集 `~/.xana/projects/*/run/serve.pid`（旧布局的 `~/.xana/run/serve.pid` 记为 default），pid 存活且 `/proc/<pid>/cmdline` 含 "serve" 才停：SIGTERM（5s 超时后 SIGKILL）；死 pidfile 清掉。
2. 同步 code / mem。
3. **迁移**：`ensure_default_project()`——默认项目已存在（`projects/default/project.json`）则跳过；否则把旧布局 `~/.xana/{workspace, sessions, runs, run, channels, scheduled_tasks.json, evolution, worktrees}` 与 `logs/serve*.log`、`logs/xana_debug_*.log` 移进 `projects/default/`，`system.json` 的 channels / daemon / sessions 拆到 `projects/default/config.json`（原文件先备份为 `system.json.bak-<时间>`；旧的 `unix_socket="~/.xana/run/serve.sock"` 改为空 = 每项目各自的 socket），会话配置文件删去已废弃的 `project_root` 字段，最后写 `project.json` 并补 daemon token。目标已存在即 `MigrationError`，sync 报错返回 1、不启动 serve；重跑从中断处继续。`--no-restart` 且仍有 serve 在运行时跳过迁移。
4. 对第 1 步停掉的每个项目用 `~/.xana/bin/xana serve --project <id>` 后台拉起（日志 `~/.xana/projects/<id>/logs/serve.log`）。

`$XANA_HOME` 的唯一解析点是 `config/paths.xana_home()`（缺省 `~/.xana`），sync/serve/boot 等全部经它取根目录（项目路径经同文件的 `project_home()` / `project_root()`）；唯一例外是 launcher 脚本——它是自包含 Python，运行时自行读 `XANA_HOME`。
