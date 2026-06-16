# xana daemon: HTTP+SSE 协议 + xanac 瘦客户端

## 目标

把 `xanac` 从"每次 `boot()`(~14.5s)"改成"瘦 client,连本机 `xana serve` 常驻进程"。同一份 daemon 协议供未来的 web / 第三方 client 复用。`xanac` 只做渲染。

参考实现:`best_agent/{codex, claude-code-2.1.88-src, openclaw, hermes-agent}` 的协议层(spec 编写时单独调研)。

## 范围 / 不在范围

**做:**
- serve 侧新增 daemon HTTP+SSE,在 gateway 上挂一个 `DaemonBridgeAdapter`(adapter_id=`"cli"`)
- xanac 重写为 daemon client,oneshot + interactive(Textual)两条路径
- `SystemConfig` 加 `daemon` 段(bind / port / token / unix_socket)
- token 首次 setup 自动生成

**不做:**
- 不重做 gateway / harness / Card / 任何渲染层
- 不上 gRPC / WebSocket(SSE 已足够,curl 友好)
- 不做 multi-user 隔离(单机单用户)
- 不做 web client(本期协议预留,后续单独 spec)

## 配置(SystemConfig 扩展)

`~/.xana/config/system.json` 新增 `daemon` 段(以下默认值):

```json
{
  "daemon": {
    "unix_socket": "~/.xana/run/serve.sock",
    "bind": null,
    "port": 17777,
    "token": "<32-byte hex>"
  }
}
```

- `unix_socket`: POSIX 默认开,`0600` 权限。设 `null` 关闭
- `bind`: 默认 `null`(不开 TCP)。设 `"127.0.0.1"` 或 `"0.0.0.0"` 启 TCP
- `port`: TCP 端口,仅 `bind != null` 时用
- `token`: 32 字节 hex(`secrets.token_hex(32)`),首次 `xana setup` 自动写;TCP 路径强制校验,Unix socket 路径靠 fs 权限信任(不校验)

校验:
- `bind=="0.0.0.0"` 时,serve 启动日志 WARN(无论 token 是否轮换过)
- 任意路径,带 `Origin` header 的请求直接 403(防同机网页 CSRF)

## 架构

```
┌──────────────┐   HTTP+SSE     ┌──────────────────────────────┐
│ xanac client │ ─────────────▶ │ xana serve                   │
│ (Textual /   │  unix sock /   │ ┌──────────────────────────┐ │
│  stdout)     │  127.0.0.1     │ │ daemon_http (aiohttp)    │ │
└──────────────┘                │ └──────────┬───────────────┘ │
                                │            │                 │
                                │ ┌──────────▼──────────┐      │
                                │ │ DaemonBridgeAdapter │      │
                                │ │ (Adapter ABC)       │      │
                                │ └──────────┬──────────┘      │
                                │            │                 │
                                │ ┌──────────▼──────────┐      │
                                │ │ Gateway / Harness   │      │
                                │ │ (跟 feishu/tg 共享) │      │
                                │ └─────────────────────┘      │
                                └──────────────────────────────┘
```

## 协议

### Endpoints

所有 POST 返回 `202 Accepted` + 空 body(收到,不代表完成)。错误返回 `4xx/5xx` + JSON `{"error": "..."}`.

| 路由 | 用途 | 入参 | 响应 |
|---|---|---|---|
| `GET /healthz` | 探活 | 无(无 token) | 200 `{"ok": true, "version": "..."}` |
| `POST /sessions/{sid}/submit` | 提交一条用户消息 | `{"text": "..."}` | 202 |
| `GET /sessions/{sid}/stream` | 订阅 SSE 事件流 | `?opt_out=draft,model_delta`(可选) | SSE |
| `POST /sessions/{sid}/cancel` | 请求取消 | `{"run_id": "..."}`(可选,空=取消当前 turn) | 202 |
| `POST /sessions/{sid}/attach` | 切某后台 run 到前台 | `{"run_id": "..."}` | 202 |
| `POST /sessions/{sid}/detach` | 把当前 run 转后台 | `{"run_id": "..."}` | 202 |
| `POST /sessions/{sid}/button` | 卡片按钮触发 | `{"action": "load_tools", "card_id": "..."}` | 202 |

`sid` 不存在 → 自动创建(BindingStore lazy-alloc 风格)。

### Auth

- Unix socket 路径:不校验 token(信任 fs 权限,`0600`)
- TCP 路径:`Authorization: Bearer <token>` 必带,否则 401
- 任何路径:带 `Origin` header → 403(SSE 浏览器 EventSource 默认不带 Origin,故同源 web client 仍然不通过 — 后续 web client 需要单独 CORS 路径)

### SSE 事件流(`GET /sessions/{sid}/stream`)

每条事件含 `id:` + `event:` + `data:`,JSON 在 data 里。

**第一帧固定是 `initialize`:**
```
id: 0
event: initialize
data: {"server_version": "0.x.y", "protocol_version": 1,
       "xana_home": "/home/.../.xana",
       "supported_methods": ["agent_event", "progress", "session.status"]}
```

**业务事件:**
```
id: <seq>
event: agent_event
data: {"kind": "draft", "session_id": "...", "harness_event": {...}, "payload": {...}}
```
```
id: <seq>
event: progress
data: {"kind": "usage_progress", "payload": {...}}
```

**session 状态:**
```
id: <seq>
event: session.status
data: {"state": "idle" | "active" | "closed",
       "subscribers": 1, "active_run_ids": ["..."]}
```

**取消完成:**
```
id: <seq>
event: turn.interrupted
data: {"run_id": "..."}
```

**心跳(每 15s,SSE 注释行,不计入回放):**
```
: heartbeat
```

### 断线 / 续传

- 每事件带 `id: <seq>`,单调递增 per-session
- server 维护 per-session ring buffer(最近 5000 条)
- client reconnect 自带 `Last-Event-ID: <seq>` header(EventSource 原生),server 从 `seq+1` 推
- ring buffer 不够覆盖(client 离线太久)→ server 推一条 `event: replay_gap` 后从最新事件继续(client 应清 Card 重新订阅)

### 背压

- 每 session SSE 队列上限 1000 条
- 溢出:server 关掉慢 client 的 SSE 连接(503),client 重连自动续传(若 ring buffer 还覆盖)
- `POST /sessions/{sid}/submit` 在 turn 进行中 → 排队(harness 内已有)

### Session 生命周期

- `submit`:启动 / 续 run(走 gateway.submit)
- `attach` / `detach`:**只动 subscriber refcount,不动 run**;run 由 cancel 杀
- session 进入 `idle`(无 active run + 无 subscriber)→ 30 min 倒计时
- 倒计时到 → emit `session.closed` → 释放内存
- subscriber 重新连 / submit 进来 → 取消倒计时

### Cancel 两段语义

1. `POST /cancel` 立刻 202(不代表已停)
2. 真正停止时 emit `turn.interrupted` SSE 事件(client 据此更新 UI)
3. 重试 `cancel` 是幂等的,不会 double-trigger

### Button 路由

`POST /button` payload 直接转给 DaemonBridgeAdapter,等价于 feishu/tg 的 callback 路由,内部映射到:
- `action == "load_tools"` → `Card.reveal_tools()`
- `action == "expand" / "expand_page" / "expand_back"` → `Card.set_expand / set_expand_page / clear_expand`
- 其他自定义 action 同样转入 Card 方法

## 改动清单

### serve 侧

**新文件:**
- `src/xana/runtime/daemon_http.py` ~250 行
  - `build_daemon_app(bridge_adapter, token, allowed_endpoints) -> web.Application`
  - SSE handler + Last-Event-ID 支持 + per-session ring buffer
  - heartbeat loop / idle unload timer
  - Origin reject / token check 中间件
- `src/xana/agent_adapter/channels/daemon_bridge.py` ~200 行
  - `class DaemonBridgeAdapter(Adapter)`:`adapter_id="cli"`
  - 内部 per-session asyncio.Queue + ring buffer
  - `render(event)` / `note_progress(payload)` → 入队
  - `submit_inbound(text)` / `cancel(run_id)` / 等转发到 gateway

**改动:**
- `src/xana/cli/serve.py`:always-on daemon_http 启动逻辑;读 `SystemConfig.daemon`
- `src/xana/config/system_model.py`:加 `DaemonConfig` 模型,挂到 `SystemConfig.daemon`
- `src/xana/cli/commands/setup_wizard.py`:首次跑时若 token 缺 → 生成并写盘

### client 侧

**重写:**
- `src/xana/cli/xanac.py`:删原 `_build_pipeline / _run / _textual` 里 in-process boot 路径
  - 新结构:
    - `_resolve_daemon_url()`:读 system.json,试 unix socket → TCP,二者皆败 → `subprocess.Popen(["xana", "serve"])` 后台拉起,poll healthz
    - `_oneshot(text, session)`:POST submit + 起 SSE 协程 → 累 UnifiedCardState → 终态打印一次
    - `_interactive(session)`:启 XanacApp,后台 SSE 协程 → 每事件 `Card.dispatch` + `app.update_card`

**新文件:**
- `src/xana/cli/_daemon_client.py` ~200 行
  - `class DaemonClient`:封 httpx + SSE,Last-Event-ID 自动续
  - `async def stream(session) -> AsyncIterator[Event]`
  - `async def submit / cancel / attach / detach / button`
  - 自动重连 + 指数退避

### 删

- 旧 `CliAdapter`(`src/xana/agent_adapter/cli.py`)— 完全删,功能移到 DaemonBridgeAdapter
- `src/xana/agent_adapter/channels/_cli_renderer_stdout.py` / `_cli_renderer_textual.py`:替换成基于 UnifiedCardState 的新渲染器(已有 `unified_card_cli.py`),旧 TurnDraft-based 路径删

### 测试

- `tests/test_daemon_http.py`:新,起 daemon + httpx client 跑 e2e
  - submit/stream/cancel happy path
  - Last-Event-ID 续传
  - Origin 拒
  - token 校验(TCP path)
  - idle 倒计时(用 monkeypatch 缩成 1s)
- `tests/test_xanac_*` 改:不再起 xanac CLI,直调 gateway(unit)或起 daemon + DaemonClient(e2e)

## 风险 / 缓解

1. **subprocess.Popen serve 时 stderr 不可见**:首次失败用户不知原因
   - 缓解:Popen 前先尝试 import boot,失败直接报错;Popen 后 5s 内 poll healthz,超时打印 `~/.xana/logs/serve.log` 末尾 20 行
2. **token 泄露**:system.json 是明文存
   - 缓解:文件 chmod 0600(setup 时强制),0.0.0.0 + 默认 token 启动日志 WARN
3. **SSE 长连接 + asyncio 调度**:大流量场景 Card 状态机可能漏事件
   - 缓解:ring buffer + Last-Event-ID 是最后兜底
4. **multi-client 同 session 状态分歧**:两个 xanac 同时连同 session
   - 缓解:允许;Card 由各 client 独立维持,看到一样的事件流;subscribers count 在 `session.status` 里露出来,后续 UI 可显示

## 成功标准

- `xanac "hi"` oneshot 端到端 < 1.5s(主要受模型 RTT 影响,daemon 开销 < 50ms)
- `xanac` 启动 → Textual UI 显示 → 输入框可输入 < 200ms(serve 已在跑前提下)
- serve 没在跑时,`xanac "hi"` 首次冷启 ≤ 16s(boot 14.5s + 0.5s daemon 启 + 1s 模型);第二次 < 1.5s
- 现有 feishu / TG e2e 全过(daemon 不动它们)
