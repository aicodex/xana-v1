---
title: cli_serve
type: doc
status: active
---

# cli_serve

`xana serve`：常驻进程入口，boot runtime、组装聊天 adapter、起 daemon HTTP+SSE、跑 inbound pump。实现在 `src/xana/cli/serve.py`（argparse 壳在 `xana.py` 的 `_run_serve`）。

## 启动顺序

1. **pidfile**：写 `~/.xana/run/serve.pid`（退出时仅当内容仍是自己的 pid 才删除），供 `xana sync` 重启和单实例判断用。
2. **workspace 解析**：`--project` 显式给出则用之；否则默认 `~/.xana/workspace`（**不是 cwd**——在源码仓库里跑 serve 不能把 dev 仓库的 README_ai.md 挂成 bot 记忆），目录按需创建并 seed 一个极简 `README_ai.md`（已存在不覆盖）。
3. **boot**：`boot(project_root=..., config_provider=DefaultConfigProvider(), history_root=~/.xana/sessions)`，拿到 gateway / scheduler / session_resolver。
4. **adapter 组装**（`build_adapters()`）：从 `system.json` 的 `channels` 读配置，环境变量覆盖（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` / `FEISHU_CONNECTION_MODE` / `FEISHU_WEBHOOK_PORT`、`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_IDS`）。飞书 api_base 按 `domain` 选 `open.feishu.cn` / `open.larksuite.com`，连接模式默认 `ws`（lark-oapi 长连接，无需公网 URL），未知值回退 ws，webhook 模式才起 aiohttp server（默认 :8765）。飞书与 Telegram 共享一个 `PairingStore`（`~/.xana/channels/pairings.json`），所以 `xana pair approve` 改动经 fingerprint 热加载进运行中的 serve，无需重启。缺必需配置的 adapter 静默跳过。
5. **daemon 单实例锁**：`daemon.bind` 必须配置——TCP 端口是 OS 级单实例锁（第二个 serve 在 pre-bind 时干净失败退出；unix socket 不能当锁，因为 UnixSite 会静默 unlink 旧的）。然后 `DaemonBridgeAdapter` 注册为 `adapter_id="cli"`，与 feishu/tg 并列。unix socket 若配置则起一个**不带 token** 的 app（本机可信），TCP app 带 `daemon.token`；bind 为 `0.0.0.0` 打 LAN 暴露警告。
6. **pump**：daemon bridge 和每个 adapter 各起一个 `_run_inbound_pump`——`async for inbound in adapter.receive()`，每条 inbound 独立 task 去 drain `gateway.submit(inbound)`（串行 pump 会让第二条消息堵到第一条收尾，gateway 的 detach 逻辑永远没机会触发）。
7. **细节接线**：Telegram adapter 把 gateway 可见命令经 `setMyCommands` 推成 `/` 菜单（飞书无此能力，靠 `/h`）；`SchedulerRunner` 的 dispatch 把到点任务包成 `InboundMessage` 走 `gateway.submit` 同一条入站路径，`adapter_id` 按 session 前缀推断（`tg-…` → telegram、`feishu-…` → feishu，无匹配退回首个 adapter）。dispatch 回调是 `_make_scheduler_dispatch(gateway, meta_service=None)`：**元 agent cron 的 marker 任务（`[xana-evolution-meta]`）先过 `meta_service.owns(text)`，命中 → `meta_service.fire()`**（declare 新 episode + 全新窗口注入元 prompt，见 [evolution_module](evolution_module.md)），不走路通 adapter 注入；serve 组装时传 `getattr(boot_result, "meta_service", None)`。
8. **SIGTERM 优雅退出**：`graceful_shutdown(gateway, grace_seconds=2.0)` 先 `gateway.cancel_all()`，等 background run drain，超时记日志强制退出（未完 run 下次启动按 shutdown_force 走 /resume）；之后停 scheduler、cancel pumps、停 daemon server 与 bridge、停 adapter、关 main_writer。

`build_adapters_from_env()` 是带空 `SystemConfig` 的兼容 shim，委托给 `build_adapters()`。`_start_daemon_in_process()` 把 boot + bridge + HTTP server 打包成协程，测试用它；`serve_main` 内联同样的积木。

## daemon HTTP 动作桥

`_make_daemon_on_action()` 把 daemon HTTP 的 action 回调接到 gateway：`cancel` → `gateway.cancel`；`attach` / `detach` 带 `run_id` 时转对应 gateway 方法；button 类动作由统一 Card 客户端自管，服务端 no-op。
