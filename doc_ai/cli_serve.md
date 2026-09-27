---
title: cli_serve
type: doc
status: active
---

# cli_serve

`xana serve [--project <id>]`：一个项目一个常驻进程——boot runtime、组装聊天 adapter、起 daemon HTTP+SSE、跑 inbound pump。实现在 `src/xana/cli/serve.py`（argparse 壳在 `xana.py` 的 `_run_serve`）。`--project` 是项目 id（缺省 `default`），`main()` 把它写入环境变量 `XANA_PROJECT`，进程内所有项目路径（`config/paths.py` 的 `project_home()` / `project_root()`）由此解析，下文 `<home>` = `~/.xana/projects/<id>`。

## 启动顺序

1. **单实例锁 + pidfile**：进程一启动就对 `<home>/run/serve.lock` 加非阻塞排他 `flock`（持有到进程退出；拿不到即退出，启动中的 serve 也持有，所以前后脚启动的第二个 serve 不会同时 boot）；拿到锁后写 `<home>/run/serve.pid`（退出时仅当内容仍是自己的 pid 才删除），供 `xana sync` / `xanac` / `project_manage` 判断运行状态与重启。
2. **项目目录解析**：`project_root()` 读 `<home>/project.json` 的 `root`（**不是 cwd**；default 项目为 `~/.xana/projects/default/workspace`）；项目未注册即退出并提示（默认项目需先 `xana setup`）。目录按需创建，缺 `README_ai.md` 时 seed 一个极简的（已存在不覆盖）。
3. **boot**：`boot(project_root=..., config_provider=DefaultConfigProvider(), history_root=<home>/sessions)`，拿到 gateway / scheduler / session_resolver；配置为用户级 system.json + 本项目 config.json 合并结果（见 [config](config.md)）。
4. **adapter 组装**（`build_adapters()`）：从合并配置的 `channels`（项目 config.json）读配置，环境变量覆盖（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` / `FEISHU_CONNECTION_MODE` / `FEISHU_WEBHOOK_PORT`、`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_IDS`）。飞书 api_base 按 `domain` 选 `open.feishu.cn` / `open.larksuite.com`，连接模式默认 `ws`（lark-oapi 长连接，无需公网 URL），未知值回退 ws，webhook 模式才起 aiohttp server（默认 :8765）。飞书与 Telegram 共享一个 `PairingStore`（`<home>/channels/pairings.json`，按项目），所以 `xana pair approve` 改动经 fingerprint 热加载进运行中的 serve，无需重启。缺必需配置的 adapter 静默跳过。
5. **daemon 端口**：`daemon.bind` 必须配置，pre-bind 该项目的 TCP 端口（端口被占即干净退出；每个项目端口不同，default 沿用 17777，新项目创建时分配空闲端口）。然后 `DaemonBridgeAdapter` 注册为 `adapter_id="cli"`，与 feishu/tg 并列。unix socket 始终起一个**不带 token** 的 app（本机可信），路径 `daemon_socket(daemon)`：`daemon.unix_socket` 为空（默认）时取 `<home>/run/serve.sock`；TCP app 带 `daemon.token`；bind 为 `0.0.0.0` 打 LAN 暴露警告。
6. **pump**：daemon bridge 和每个 adapter 各起一个 `_run_inbound_pump`——`async for inbound in adapter.receive()`，每条 inbound 独立 task 去 drain `gateway.submit(inbound)`（串行 pump 会让第二条消息堵到第一条收尾，gateway 的 detach 逻辑永远没机会触发）。
7. **细节接线**：Telegram adapter 把 gateway 可见命令经 `setMyCommands` 推成 `/` 菜单（飞书无此能力，靠 `/h`）；`SchedulerRunner` 的 dispatch 把到点任务包成 `InboundMessage` 走 `gateway.submit` 同一条入站路径，`adapter_id` 按 session 前缀推断（`tg-…` → telegram、`feishu-…` → feishu，无匹配退回首个 adapter）。dispatch 回调是 `_make_scheduler_dispatch(gateway, meta_service=None)`：**元 agent cron 的 marker 任务（`[xana-evolution-meta]`）先过 `meta_service.owns(text)`，命中 → `meta_service.fire()`**（declare 新 episode + 全新窗口注入元 prompt，见 [evolution_module](evolution_module.md)），不走路通 adapter 注入；serve 组装时传 `getattr(boot_result, "meta_service", None)`。
8. **SIGTERM 优雅退出**：`graceful_shutdown(gateway, grace_seconds=2.0)` 先 `gateway.cancel_all()`，等 background run drain，超时记日志强制退出（未完 run 下次启动按 shutdown_force 走 /resume）；之后停 scheduler、cancel pumps、停 daemon server 与 bridge、停 adapter、关 main_writer。

`_start_daemon_in_process()` 把 boot + bridge + HTTP server 打包成协程，测试用它；`serve_main` 内联同样的积木。

## daemon HTTP 动作桥

`_make_daemon_on_action()` 把 daemon HTTP 的 action 回调接到 gateway：`cancel` → `gateway.cancel`；`attach` / `detach` 带 `run_id` 时转对应 gateway 方法；button 类动作由统一 Card 客户端自管，服务端 no-op。
