---
title: cli_xanac
type: doc
status: active
---

# cli_xanac

`xanac`：交互聊天客户端。它不是独立 runtime——是 `xana serve` daemon 的 HTTP+SSE 瘦客户端（替换了过去每次调用 in-process `boot()` 的架构）。实现在 `src/xana/cli/xanac.py`（one-shot + 编排）、`xanac_textual.py`（TUI）、`_daemon_client.py`（HTTP 客户端）。

## endpoint 解析与 serve 自动拉起

`_resolve_endpoint()` 读 `~/.xana/config/system.json` 的 `daemon` 段：unix socket 配了且文件存在 → `http://unix`（无 token）；否则 TCP `http://<bind>:<port>`（带 token）；bind 为空回退 `127.0.0.1`。

`_ensure_serve_running()`：unix / TCP 都不在监听就 `subprocess.Popen` 拉起 `~/.xana/bin/xana serve`（不存在则用 PATH 里的 `xana`），日志追加到 `~/.xana/logs/serve.log`，轮询 healthz 最多 20s，超时报错并附日志尾部。

## session 解析

显式 `--session` / `--random-session`（生成 `cli-<hex>`）优先；默认 session 先问 daemon 的 `GET /resolve?adapter=cli&thread=`——跟随 `/clear` 轮换后的当前 sid，daemon 未接 resolver 时退回 `default`。

## one-shot 路径

`xanac "问题…"` 或管道输入：POST `/sessions/{sid}/submit` 后消费 SSE，把事件喂进 `UnifiedCardState`（[agent_adapter](agent_adapter.md) 的统一卡片状态机，`apply_event` 归并 agent_event 与 progress），在终结事件上 `render_card()` 打印一次。

- **终结判定**：`agent_event` 的 `final` / `error` / `cancelled`（rc 分别 0/1/0），以及 progress 的 `compact_finalized` / `compact_cancelled`（/compact 无 agent final）。
- **replay-vs-live 门**：先 drain 0.5s 让 replay 阶段过去并快照 `baseline_seq`，之后只认 seq > baseline 的终结事件，避免上一轮 turn 的 final 抢先结束本轮。
- 超时 120s → rc 2；SSE 事件重建为 `AgentEvent` / `ProgressEvent`（wire payload 与 `daemon_bridge._agent_event_to_dict` 镜像）。

## TUI 路径

`-i` / 无参数且 stdin 是 TTY：`run_textual_with_daemon()` 起 `XanacApp`（Textual）。左侧 fg-pane 渲染当前卡片（`render_widgets`），右侧 tab-pane 列 run 列表，底部 Input。

- **渲染防抖**：每个 SSE 事件算 `_state_hash`（段落拓扑 + 内容长度 + 按钮/footer 指纹），没变直接跳过；变了也不立刻重建，50ms throttle 合并流式 delta 后一次性 `remove_children + mount`。
- **输入**：回车 → `DaemonClient.submit`；卡片按钮 `send` → submit，`load_tools` / `expand` → POST `/button`，其余按 detach 处理；tab 选中 → POST `/attach`。
- 按键：Tab 切焦点到 run 列表，Esc 回输入框，Ctrl-C 退出。

## DaemonClient

`_daemon_client.py` 的 `DaemonClient`（httpx）：`stream()` 订阅 `/sessions/{sid}/stream`，带 `Last-Event-ID` 断线续传 + 自动重连（连接类错误 sleep 0.5 后重连）；`submit` / `resolve` / `cancel` / `attach` / `detach` / `button` 对应各 POST/GET。wire 帧解析只认 `id:` / `event:` / `data:` 行，注释行忽略。
