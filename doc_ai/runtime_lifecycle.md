---
title: runtime lifecycle
type: doc
status: active
---

# runtime lifecycle

## 上级模块

- [runtime](runtime.md) — 模块设计

## 代码锚点

- [serve.py](../src/xana/cli/serve.py) — `xana serve` 进程生命周期编排
- [daemon_http.py](../src/xana/runtime/daemon_http.py) — HTTP+SSE daemon app 构造
- [interfaces.py](../src/xana/runtime/interfaces.py) — `ComponentLifecycle` / `Runtime`（仅接口合约，无实现）

## 职责

进程级生命周期：`xana serve` 的启动（preflight → boot → adapters → daemon HTTP）、运行中健康维持（心跳 / idle 卸载 / 调度轮询）、关闭（SIGTERM → `cancel_all` → 任务回收）。`runtime/interfaces.py` 的 `ComponentLifecycle`（install/start/stop/uninstall）与 `Runtime`（boot/shutdown/health）是早期接口合约，当前**无实现**；真实生命周期由 `serve.py` 的函数体承担，`RuntimeHealth`/`RuntimePhase` 等 contract 仅作为数据类型保留。

## 启动序列（serve.py）

1. **preflight**：先 bind `(daemon_cfg.bind, daemon_cfg.port)` 探测再释放——TCP 端口是 OS 级单实例锁，第二个 `xana serve` 在抢占 unix socket 之前就会 EADDRINUSE 失败（UnixSite 会静默 unlink 旧 socket，不能当锁用）。
2. **boot()**：调 [runtime_boot](runtime_boot.md) 的 `boot()` 拿 `BootResult`。
3. **adapters**：`build_adapters(...)` 按 system.json 的 channels 配置构造 feishu/telegram adapter，注册进 gateway；每个 adapter 一条 `_run_inbound_pump` task（每条 inbound 独立 task，detach 才有机会触发）。
4. **daemon HTTP**：构造 `DaemonBridgeAdapter` 并注册为 `cli` adapter，然后双通道：
   - **unix socket**（`daemon_cfg.unix_socket`，默认 `~/.xana/run/serve.sock`）：`build_daemon_app(bridge, token="", ...)` + `run_unix_socket`（0600 权限）。本地通道不设 token——文件权限即边界。
   - **TCP**（`daemon_cfg.bind:port`，默认 `127.0.0.1:17777`）：`build_daemon_app(bridge, token=daemon_cfg.token, ...)` + `run_tcp`。`bind == "0.0.0.0"` 时打印 LAN 暴露警告。
5. **SchedulerRunner**：`boot_result.scheduler` 非空时启动轮询任务（详见 [runtime_scheduler](runtime_scheduler.md)）。
6. 无 chat adapters 时退化为 daemon-only 模式，挂起等待 shutdown。

## daemon HTTP（daemon_http.py）

`build_daemon_app(*, bridge, token, on_action, heartbeat_interval_s=15.0, idle_unload_s=1800.0)` 返回 aiohttp app：

- **中间件两道**：`origin_reject`（带 `Origin` 头的请求一律 403，挡浏览器跨站）→ `auth`（`/healthz` 豁免；`token` 为空则放行；否则校验 `Authorization: Bearer <token>`，不符 401）。
- **路由**：
  - `GET /healthz` → `{"ok": true, "version"}`，免鉴权；
  - `GET /resolve?adapter=cli&thread=` → 问 `SessionResolver` 当前 sid（客户端 `/clear` 轮转会话前用来对齐）；
  - `POST /sessions/{sid}/submit` → `bridge.submit_inbound`，202；
  - `GET /sessions/{sid}/stream` → SSE 流：先写 `initialize` 事件（server/protocol 版本、supported_methods），再写当前 `session.status`，然后进入 `bridge.subscribe(sid, last_event_id=...)` 的事件泵；支持 `Last-Event-ID` 断线续传；heartbeat 每 15s 一条注释行；
  - `POST /sessions/{sid}/{action:cancel|attach|detach|button}` → `on_action(sid, action, payload)` 异步触发（202），由 serve 桥到 gateway.cancel / attach / detach / Card.dispatch。
- **idle 卸载**：app on_startup 起 `_idle_loop`，每 `min(max(idle_unload_s/2, 1), 60)` 秒调 `bridge.sweep_idle(idle_unload_s)`，空闲 session 回收。

## 关闭序列

SIGTERM/取消 → `gateway.cancel_all()`（翻所有 inflight run 的 abort signal，标 `exit_reason=shutdown_graceful`）→ gather 回收 daemon/adapter/scheduler 任务 → `daemon_bridge.stop()` → 进程退出。中途单个请求失败不杀死进程；`SchedulerRunner` 与 SSE heartbeat 的异常均就地记日志。

## 不变量

- TCP 端口永远绑定（单实例锁），即使走 unix socket 模式。
- unix socket 通道不设 token；鉴权边界是 0600 文件权限。
- `/healthz` 是唯一免鉴权端点，其余一律过 Origin 拒绝 + Bearer 校验。
- 关闭时先翻 abort 再回收任务，保证 inflight run 落盘 exit_reason。

## 验收要求

- 输入/输出与 [runtime](./runtime.md) 的模块边界一致。
- 不引入隐藏状态权威。
- 不绕过 [replacement_rule](replacement_rule.md)。
