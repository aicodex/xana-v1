# xana

一个**多通道 AI 助手运行时** —— 通过飞书 / Telegram / CLI 三个渠道服务同一个用户,共享同一份会话上下文、同一套 agent 能力。核心目标是把"CC 风格的 turn loop + 工具生态 + async 卡片"做成生产可用的 chat runtime。

## 特性

- **统一卡片模型 (MVVM)** —— 飞书 / TG / CLI 三个 View 共用同一个 `UnifiedCardState`,一份状态机三份渲染
- **Compact-as-PH 上下文压缩** —— 手动 `/compact` 与自动压缩走同一 PH 协议 + 会话级串行锁,消除 race
- **异步多 Run 并发** —— 用户任何时候可发新消息,前一 Run 转后台继续跑,前端卡片按 PH 冻结/终态回写
- **ContextSDK 单入口** —— 所有上下文操作走一个 facade,代码层防止裂脑(见设计文档 §3)
- **平台无关序列化** —— Message ABC 各子类提供 `to_anthropic()` / `to_openai()`,内部元数据永不进入模型 prompt
- **契约锁定的回归网** —— 23 项不变量 + 14 个 ultimate race scenario,`pytest -m merge_gate` 一键跑

## 快速上手

```bash
xana setup     # 初始化 ~/.xana(配置 + 代码副本 + 内存挂载 + PATH + 默认项目 default)
xana serve     # 启动默认项目的后端 daemon(单实例锁 + TCP 端口 + Unix socket)
xanac "你好"   # CLI 一发一答,自动走 daemon SSE
xanac -i       # Textual 交互式 REPL
```

**项目**:一个项目 = 一套记忆 + 自己的 agent / 工具默认配置 + 一个 serve 进程(通常接一个飞书机器人)。安装即有默认项目 `default`。其他项目用 `xana project create <id> [--root <目录>]` / `xana project list` 管理,或在 default 项目的聊天里让 xana 用 `project_manage` 创建并接机器人;`xana serve --project <id>`、`xanac --project <id>` 选择项目。项目根目录的 `AGENTS.md` 是项目说明,该项目的每个会话都会读到(改完下一条消息生效)。设计见 [docs/design/project-design.md](docs/design/project-design.md)。

`~/.xana` 布局:用户级 `config/system.json`(模型、搜索、策略)、`credentials/`、`agents/`、`skills/`、`code/`、`bin/`、`mem/`;每个项目的私有数据在 `projects/<id>/`(`project.json`、`config.json` 含机器人凭据 / daemon 端口 / 会话映射、`sessions/`、`runs/`、`logs/`、`run/`、`channels/`、`scheduled_tasks.json`、`evolution/`、`worktrees/`);default 项目的项目目录是 `projects/default/workspace`。

飞书 / Telegram 通道在项目配置 `~/.xana/projects/<id>/config.json` 的 `channels` 配置 bot 凭据(`xana setup` 向导写默认项目)后由该项目的 `xana serve` 自动接入。

## 目录结构

```
xana/
├── src/xana/               # 运行时源码
│   ├── context/            # ★ ContextSDK — 唯一上下文入口 + Message 类 + 平台序列化
│   ├── memory/             # 主上下文存储、写队列、bridge、history mirror
│   ├── harness_sdk/        # Turn loop 编排 + 系统 prompt + auto-compact
│   ├── agent_adapter/      # Adapter ABC + 三个 channel + 命令路由 + gateway
│   │   └── channels/       #   feishu / telegram / cli daemon bridge + unified_card 模型
│   ├── agents/             # Agent 契约 + 默认 ChatAgent
│   ├── llm_driver/         # ModelClient ABC + Anthropic/DeepSeek/OpenAI 适配 + FakeModelClient
│   ├── tools/              # 内置工具(bash/read/edit/write/glob/grep/webfetch/...)+ 权限/沙箱
│   ├── skills/             # Skill 元数据 + 激活 + 注入
│   ├── extension_api/      # Contributor ABC + Registry + PromptAssembler
│   ├── runtime/            # boot()、SessionResolver、daemon HTTP+SSE、定时任务调度、项目管理
│   ├── cli/                # xana CLI 命令 + xanac(CLI 客户端)+ Textual REPL
│   ├── config/             # 路径解析(paths.py)+ system.json / 项目 config.json 分层加载 + schema
│   ├── session_config/     # 会话级配置(model/effort/streaming/thinking...)
│   ├── state/              # SessionStore + RunStore + FileHistoryMirror
│   └── policy/             # 权限模式 (default/plan/write)
│
├── docs/design/            # ★ 终极版设计文档 —— 单一来源,先经用户确认才可改
│   ├── session-and-card-state.md   # session / run / card 状态机 + 23 项不变量
│   └── channels/           # 各 channel 渲染差异(feishu/telegram/cli)
├── docs/superpowers/       # 早期 spec 存档(design/ 是权威)
│
├── tests/                  # 独立 git 子仓,merge_gate 契约测试为主
│   ├── e2e_real/           # 契约测试 + card frame recorder
│   ├── context/            # ContextSDK / Message 单元测试
│   ├── test_compact_race_ultimate.py  # 14 场景终极回归网
│   └── ...
│
├── README.md               # 本文件
├── README_ai.md            # AI 记忆入口(挂载到 ~/.xana/mem)
├── CLAUDE.md               # 项目规则(CI 强制)
└── pyproject.toml
```

## 核心概念

**Session** — 一个持久对话的身份,由 `(adapter_id, thread_id)` 映射到 `session_id`。/clear = 换 sid,旧 session 内存留底。

**Run** — 一次 LLM turn 的执行单元,从主上下文 fork 快照后台异步跑。前端卡片是 Run 的实时镜像。

**PH (Placeholder)** — 主上下文中的占位符;Run 完成后 replace 成真实内容。两类:
- `RegularPlaceholder` — 用户 turn 的占位,模型可见 1 条 assistant
- `CompactPlaceholder` — compact 运行期间的占位,模型完全看不见(隐身)

**Card** — 一张飞书/TG/CLI 卡片,与 Run 一对一绑定(1 Run ↔ 1 PH ↔ 1 Card 终生绑定)。

## 开发者规则(CI 强制)

见 [CLAUDE.md](CLAUDE.md):

1. 永远使用现有接口,不够则先与用户确认再加
2. contracts / interfaces / implementation 三层
3. `boot()` 是唯一组合点
4. 构造注入,禁全局单例
5. 每个文件独立可 import

**回归契约**:任何改动前后必须 `pytest -m merge_gate` 通过 39+1(见 [docs/design/session-and-card-state.md](docs/design/session-and-card-state.md) §9)。

## Debug 开关

```bash
export XANA_DEBUG_ACTCTX=1     # 主上下文每次读写完整快照写日志
export XANA_DEBUG_COMPACT=1    # compact lock/event/collapse trace
```

日志:`~/.xana/projects/<id>/logs/xana_debug_actctx.log` / `xana_debug_compact.log`

## License

Private / internal.
