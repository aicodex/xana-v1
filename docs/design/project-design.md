---
title: project:一个项目一套记忆与作用域,一个项目一个 serve
status: active
date: 2026-09-27
---

# project

## 1. 目标

- project = 一套记忆 + 自己作用域的 agents / skills / 工具配置,下面有多个会话(每个会话由一个 agent 接管,`/agent`)。
- 一个 project 一个 serve 进程,默认接一个飞书机器人;以后 Web 端做多会话选择时,会话以 project 为第一层。
- 安装即有默认项目 `default`,就是现在聊天用的这一个,放在 `~/.xana` 下。

## 2. 参考(Codex / Claude Code 源码调研)

- 两家的 project 都不是一等实体,而是由路径推出的 key;按项目隔离的只有会话、信任状态和仓库里的指令 / 配置;凭据、日志用户级(Codex 记忆也是全局)。
- 配置分层 user < project,项目层不能改敏感项(Codex 强制剔除 provider、base_url 等);agents / skills 同名时项目级覆盖用户级;恢复会话默认只列本项目。
- 两家后台都是一个用户级进程服务所有项目;xana 选"一个项目一个进程":现有 boot 就是单项目组装,改动小,项目间天然隔离。多进程共用 Kimi 凭据已有文件锁 + 重读磁盘(refresh_token 每次轮换,不能各复制一份)。
- 不照搬的:按 cwd / git 根自动识别(常驻服务的 cwd 没有语义)、会话与记忆用两个不同 key、交互式信任弹窗。

## 3. 目录布局

```
~/.xana/                              用户级(所有项目共享)
  config/system.json                  模型、搜索服务、策略等全局配置(不再含机器人 / 端口 / 会话映射)
  credentials/                        模型登录凭据(Kimi OAuth)
  agents/  skills/                    用户级通用 agent / skill
  code/  bin/  mem/                   安装与同步产物
  projects/<id>/                      项目私有数据(不进任何仓库)
    project.json                      {"root": 项目目录}
    config.json                       本项目的 channels(机器人凭据)、daemon(端口 / socket)、sessions(渠道→会话映射)、tools
    sessions/  runs/  logs/  run/     会话、运行记录、日志、单实例锁 / pid / socket
    channels/                         渠道绑定、配对
    scheduled_tasks.json  evolution/  worktrees/
<项目目录>/                           项目内容(default 项目 = ~/.xana/projects/default/workspace)
  README_ai.md + 记忆树               本项目的记忆
  .xana/agents/  .xana/skills/        项目级 agent / skill
```

- 每个项目的私有数据集中在 `projects/<id>/`;项目目录可以是任意路径(比如一个代码仓库),只放可以公开的内容。
- 路径 API(`config/paths.py`,唯一解析点):`xana_home()` 用户级;`project_id()` = 环境变量 `XANA_PROJECT`,缺省 `default`;`project_home(id)` = `~/.xana/projects/<id>`;`project_root(id)` = `project.json` 的 root。`xana serve --project <id>` 设置 `XANA_PROJECT`,进程内所有项目路径由此解析。

## 4. 配置分层

- 用户级 `system.json`:models / default_model / model_tiers / limits / web_search / mcp / policy / tools。
- 项目级 `projects/<id>/config.json`:channels、daemon、sessions、tools、agent_defaults(§8)。
- 合并:加载 `system.json` 后,用项目配置覆盖允许的键(channels、daemon、sessions、tools、agent_defaults);项目配置出现其他键报错(模型、凭据、搜索服务地址、策略不能由项目改)。
- `daemon.unix_socket` 未配置时取 `projects/<id>/run/serve.sock`;`daemon.port` 每个项目不同(default 沿用 17777,新项目创建时分配空闲端口)。
- `SessionResolver` 的渠道→会话映射写回项目 `config.json`;项目 `config.json` 权限 600(含机器人凭据)。

## 5. 命令

| 命令 | 行为 |
|---|---|
| `xana setup` | 除原有步骤外,创建默认项目(`projects/default/`、`project.json`、`config.json`、`workspace/README_ai.md`);旧布局就地迁移(§6) |
| `xana serve [--project <id>]` | 以某个项目运行;缺省 default。`--project` 由"项目目录路径"改为"项目 id"(目录由 `project.json` 决定) |
| `xana project create <id> [--root <目录>] [--port N]` / `xana project list` | 注册新项目(不给目录时放在 `projects/<id>/workspace`)/ 列出项目(含是否在运行);日常用聊天里的 `project_manage`(§7),命令行是保底 |
| `xana sync` | 停止所有在运行的项目 serve → 同步代码 → 迁移(若需要)→ 逐个重启 |
| `xanac [--project <id>]` | 连接某个项目的 serve;缺省 default |
| `xana pair ... [--project <id>]` | 配对数据按项目 |
| `xana login / logout` | 不变(用户级) |
| 聊天里 `/project` | 改为只读:显示当前项目 id 与目录(会话不能换项目;`SessionConfig.project_root` 删除,它原本就没有被读取) |

## 6. 迁移(一次性,幂等)

旧布局 → 默认项目,由 `xana setup` 与 `xana sync` 调用(sync 在停掉 serve 之后、重启之前执行,迁移时没有进程在写):

1. `~/.xana/workspace` → `projects/default/workspace`;
2. `sessions/`、`runs/`、`logs/serve*.log`、`run/`、`channels/`、`scheduled_tasks.json`、`evolution/`、`worktrees/` → `projects/default/` 下同名位置;
3. `system.json` 的 channels / daemon / sessions 移到 `projects/default/config.json`,原文件先备份为 `system.json.bak-<时间>`;
4. 已迁移(`projects/default/project.json` 存在)则跳过;任何一步失败即停止并报告,不做半迁移后继续启动。

`race_sessions*` 等测试残留不迁移,单独询问后清理。

## 7. 在聊天里管理项目(shell 只作保底)

首次安装之后,用户尽量不用再回 shell:default 项目是"管理台",带项目管理工具与技能。

### 7.1 `project_manage` 工具(只在 default 项目注册,工具组 `projects`)

| 动作 | 说明 |
|---|---|
| `list` | 项目、目录、端口、是否在运行、是否已接飞书机器人 |
| `create` | 名字(必填)、目录(可选,缺省 `projects/<名字>/workspace`)、`agent_defaults`(可选,见 §8) |
| `configure` | 修改项目的 `agent_defaults`;名字写错(未知的工具 / 组 / 技能)时报错 |
| `bind_feishu` | 发起飞书"扫码创建应用"(设备码流程),立即返回授权链接;后台轮询,确认后把机器人凭据写进该项目配置,并把确认人(本应用下的 open_id)直接加入该项目的配对白名单 |
| `feishu_status` | 等待 / 查看绑定结果(最多等 2 分钟,执行中上报进度) |
| `start` / `stop` | 启动 / 停止该项目的 serve(`xana serve --project <名字>`,后台进程) |

- 飞书卡片里的链接可点击,手机上在飞书内置浏览器打开即可确认(已登录),不必另一台设备扫码。
- 授权链接由模型写进回复发给用户;用户确认后说一声,模型再调 `feishu_status`。
- 不能停 / 删 default 项目自己;删除项目本次不做(误删代价大,命令行也不提供)。

### 7.2 `project_creator` 技能(内置,中文)

流程:弄清项目要做什么 → 起名字(目录默认即可)→ 按用途选工具 / 技能的默认可见性(`project_manage` 可列出可选的组与技能)→ 与用户确认 → `create` → `bind_feishu` 发链接 → 用户确认后 `feishu_status` → `start` → 告诉用户去新机器人那里聊。

## 8. 项目给 agent 的默认配置(`agent_defaults`)

- 项目配置新增键 `agent_defaults`:`tools`(工具选择表达式,缺省 `["default"]`)、`disallowed_tools`、`skills`(技能选择,缺省全部)。
- 在该项目中:内置 chat,以及 AGENT.md 没写 `tools` / `skills` 的 agent,按项目默认;写了的以 agent 自己为准。不写就是"跟随项目默认",项目默认改了这些 agent 随之改变。`agent_define` 新建 agent 不填 tools / skills 即跟随项目默认。
- **技能可见性**落地(此前 `allowed_skills` 只是声明):`AgentSpec.skills: tuple | None`(None = 全部,`()` = 无,名单 = 只这些,`*` = 全部)取代 `allowed_skills`;技能列表只列可见技能,`skill_load` 拒绝不可见技能(与工具的"可见 + 执行前校验"一致)。

## 9. 不在本次范围

- 项目级 skills 的加载(目录已预留,与 agents 同样的两级规则,下一步做);项目信任机制(项目目录目前都是自己的);Web 端多会话。

## 10. 验证

- 路径 API 单测:XANA_PROJECT 切换、default 的 root、未注册项目报错。
- 配置合并:允许键覆盖、禁止键报错、会话映射写回项目配置。
- 迁移:在临时 XANA_HOME 构造旧布局 → 迁移结果逐项核对 → 再跑一次无变化;失败时不留半状态。
- 两个项目同时 serve:不同端口、不同锁、互不影响;`xana sync` 重启全部。
- 全量测试 + compact 回归网;部署:在真实 `~/.xana` 执行迁移前先完整备份,部署后 CLI / 飞书实测。
