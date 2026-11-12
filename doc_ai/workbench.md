---
title: workbench
status: active
---

# Web 工作台

当前源码包含默认排队/选择性插话、会话查询、三档会话权限/审批、持久 goal/checklist、后台子会话、插话/取消和命令日志。Vue 页面由原 daemon 提供，不创建第二条执行链。尚未部署。完整范围和阶段记录见[实施计划](../docs/design/web-workbench-implementation-plan.md)。

## 入口和所有权

- `workbench/contracts.py`：SessionView、ConversationTurn、ConversationPage，只是展示投影。
- `workbench/conversations.py::ConversationService`：按项目查询主/子会话、分页 run 历史、父子关系和运行中内容。构造注入 SessionStore、RunStore、RunJsonlWriter 和 RunEvents。
- `harness_sdk/events.py::RunEvents`：当前进程的有界回放、活动流式内容和观察者；由 TurnCoordinator 发布执行事件，RunJsonlWriter 发布已提交行。不拥有执行或可写聊天历史。
- `runtime/workbench_http.py`：现有 daemon 内的 GET `/sessions`、`/sessions/{sid}/conversation`、`/sessions/{sid}/events`。`boot()` 构造查询服务，serve 显式传入。
- `policy/approvals.py`：权限与审批唯一服务；配置仅写本会话，缺省 yolo。子会话权限不得高于父级的有效设置（包括缺省值），不批量改写存量配置。
- `state/todos.py`：现有清单文件路径不变，工具和 Web 共用；当前清单在压缩后由 ContextSDK 提交到摘要。
- `harness_sdk/goal.py`：目标文件、状态、预算用量与幂等计量。create/get/update_goal 取代 report_goal 和独立模型评估请求。
- `state/process_logs.py`：完整命令输出；按 execution_id 隔离，provider call_id 可重复。offset 为 JSONL 物理字节边界；分页、tail 与纯文本下载共用记录。
- `workbench/media.py`：浏览器上传句柄与会话归属；现有 FileMediaStore 负责格式、大小与快照。客户端不能传任意服务端 file_path。模型能力从现有 model client 的配置化 capabilities 读取，图片 10 MiB，视频 100 MiB。
- `web/src`：Vue 3/TypeScript 的页面与可重建投影，fetch SSE 传 Bearer。构建产物在 `workbench/static`，Python 包携带，无 Node 运行时依赖。

模型上下文仍由 ContextSDK 管理。展示历史读取每个 run 的持久 JSONL，而不是被压缩替换的主历史。RunJsonlWriter 按 row_id 幂等写入、fsync；主历史写入保留 run_id，启动时对账补齐被中断的 audit append。旧记录无 run_id 时不猜测归属；缺失或损坏记录通过 history_complete/issues 显示。

## 订阅契约

每个 session 的 cursor 是随机 epoch 与序号；进程重启、缓存淘汰、跨会话、游标超前/过旧均重置为 snapshot。有效游标回放 execution 事件。新订阅先登记观察者再读取历史，避免查询期间漏事件；行按稳定 row_id 合并，快照与后续事件可能包含同一行，客户端须按身份覆盖。

`model_delta.payload.item_id` 与最终 assistant row_id 一致；提交后用持久行替换活动片段。run_closed 结束活动投影，不改变后台任务。慢订阅者超过有界队列时关闭该观察流，重连后回放或重新获取快照。关闭观察者不请求取消。

浏览器同源请求仍需 Bearer 认证；跨源拒绝，无 token 的 Unix/测试入口仍拒绝 Origin。原 CLI HTTP/SSE 用途继续存在，两个传输观察同一个执行链。

## 操作与边界

当前项目服务地址的 `/` 提供页面，输入项目 daemon token 后选择或创建会话；新会话默认“必要时询问”。模式选择、审批回复、目标暂停/恢复/取消和任务停止均走独立控制请求。多项目仍是各自服务与 token，不以一个 token 访问全部工作区。

主/子面板同时订阅各自会话；关闭面板仅关闭观察。默认发送进入持久 queue，显示等待本轮结束；点击“立即插话”将对应项提升为 steer，刷新会重建。普通命令继续运行，可唤醒的任务等待返回当前进度。控制端点为 POST `/sessions/{sid}/inputs/{input_id}/steer`；已消费或不属于该会话的消息不会被重新插入。停止按钮绑定当前显示的 run ID，不误停后续队列项。页面不支持编辑子 agent，也没有 swarm/tower。日志页面只保留有界显示片段，完整日志由下载提供。

Goal 沿同一 run 的原模型/工具循环续跑，以 steps 计量；Kimi 的自动后续 turn 在此适配为循环步骤。普通对话不自动创建 goal。模型请求完成后先走既有 verify；未通过则继续，取消/错误/显式轮数限制则暂停，预算达到则 blocked。重启只恢复目标状态并暂停活动目标，不重放执行副作用。定时任务生命周期不变。

命令仍在前台调用内执行，没有新增 Bash 转后台。完整输出落盘，但 ProcessRunner 仍按既有语义累计内存输出；日志没有自动清理策略，需纳入后续长期运行容量管理。未声称此次已解决无限输出、磁盘配额或 24h 运维的全部问题。

开发构建：在 `web/` 运行 `npm ci --ignore-scripts`、`npm run build`；启动仍使用现有 `xana serve`，TCP 浏览器访问需要配置 token。测试依赖新增 Tree-sitter 解析器，生产需随正常包安装更新，不能仅复制 Python 文件。

## 验证

`tests/test_workbench_conversations.py`覆盖回放/快照/持久历史；`test_workbench_permissions.py`覆盖权限、父子继承、审批和取消；`test_workbench_goals.py`覆盖目标恢复、旧请求、预算和用户停止；`test_workbench_process_logs.py`覆盖真实命令、重复 call_id 取消隔离、UTF-8、完整输出和磁盘失败收尾；`test_workbench_media.py`覆盖浏览器媒体边界；`test_input_queue.py`覆盖队列 FIFO、选择性提升、断开观察、恢复、真实 Bash、任务等待与聊天按钮作用域。`web/tests/workbench.spec.ts` 在 Chromium 中连接隔离的真实 harness/HTTP/存储，显式脚本模型测试审批、插话、目标、清单、子会话、日志、媒体与 XSS 过滤。验收数量与真实模型结果见实施计划。
