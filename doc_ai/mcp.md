---
title: mcp
type: doc
status: active
---

# mcp

## 定位

外部 MCP server 的配置、连接、工具审核与发布。外部工具定义**不当作可信提示词**：每次发现都先经确定性粗审，再进入现有工具目录与执行链（复用 `Tool` ABC、`tool_search` 延迟披露、`PermissionService` 审批，不新增第二套执行/权限/披露机制）。设计见 [MCP 连接、工具审核与热加载设计](../docs/superpowers/specs/2026-11-13-mcp-design.md)（status: active，与实现一致；差异与已知限制见本文「边界与限制」）。

## 代码锚点

- [contracts.py](../src/xana/mcp/contracts.py) — `ConfiguredServer` / `McpConfigSnapshot`（不可变快照，深层只读）、`McpCallResult`、`ServerView` / `ToolView`（脱敏视图）。
- [config.py](../src/xana/mcp/config.py) — `McpConfigStore`（load / write_project_server / delete_project_server / write_project_override / versions / audit_store_path）与 `expand_environment`（`${ENV}` 凭据展开）。
- [client.py](../src/xana/mcp/client.py) — `McpClientConnection`（官方 `mcp` SDK 适配：stdio / streamable-http / sse 三分支）、`McpTransportError`（传输失效判定）。
- [manager.py](../src/xana/mcp/manager.py) — `McpManager`，唯一连接与发布状态机（start / close / reload / register_server / remove_server / set_availability / list / `load_error`）。
- [audit.py](../src/xana/mcp/audit.py) — `audit_tool`（结构检查 + 注入粗审，纯确定性）、`tool_fingerprint`；常量 `RAW_NAME_MAX=128`、`TOOL_DEF_MAX_BYTES=32*1024`、`DESCRIPTION_MAX_CHARS=4000`。
- [audit_store.py](../src/xana/mcp/audit_store.py) — `AuditStore`，只追加脱敏 JSONL。
- [naming.py](../src/xana/mcp/naming.py) — `qualified_tool_name`：`mcp__{server}__{tool}` 清洗，超 64 字符截断并追加清洗后全名 SHA-256 前 8 位。
- [tools.py](../src/xana/mcp/tools.py) — `build_tool_spec`、`McpTool`（现有 `Tool` ABC 实现）、`convert_mcp_result`。
- [mcp_http.py](../src/xana/runtime/mcp_http.py) — daemon 内 `/mcp*` 管理 API；[cli/commands/mcp.py](../src/xana/cli/commands/mcp.py) — `xana mcp list/reload`。

## 配置契约

配置模型在 `config/system_model.py`：`McpServer`（frozen，`extra="forbid"`；transport 为 `stdio | http | sse`，按 transport 校验必填/互斥字段）、`McpServerOverride`、`Mcp`。`PROJECT_KEYS` 含 `mcp`，项目级 `config.json` 可声明 server。

用户级 `~/.xana/config/system.json` 示例：

```json
{
  "mcp": {
    "servers": [
      {
        "name": "demo",
        "transport": "stdio",
        "command": "python3",
        "args": ["-m", "mcp_demo_server", "--control", "/tmp/demo.ctl"],
        "env": {"DEMO_TOKEN": "${DEMO_TOKEN}"},
        "deferred": true,
        "startup_timeout_s": 15,
        "tool_timeout_s": 60
      },
      {
        "name": "remote",
        "transport": "http",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "${MCP_REMOTE_TOKEN}"}
      }
    ],
    "overrides": {"demo": {"enabled_tools": ["echo"]}}
  }
}
```

项目级 `projects/<id>/config.json` 同结构；`merge_mcp_configs`（唯一合并权威，boot 路径与运行期热重载共用）：项目同名 server 整体替换用户连接定义，override 按 name 字段级合并（项目优先），override 应用到合并后的同名 server，无对应 server 的 override 保留但显示未匹配。`tools.disabled` 全局并集规则对 `mcp` 组名与工具全名同样生效。

override 只含 `enabled` / `enabled_tools` / `disabled_tools` / `deferred`：`enabled_tools=None` 表示全部，`[]` 表示全不选，随后再减 `disabled_tools`；override 不能改命令、URL 或凭据。

- 凭据引用只支持 `${ENV_NAME}` 从 serve 进程环境展开 env/header 值；未设置变量或 `${secret:...}` 使**该 server** 进入 `config_error`（其余 server 正常加载，manager 绝不连接它）；坏 JSON/坏 schema 整体拒绝并保留上一份快照。API 只返回脱敏结构，不回显 env/header 值。
- stdio 子进程环境 = **serve 进程完整环境**叠加 server `env`（`{**environ, **server.env}`）；未写 `cwd` 时用项目 workspace。**同一用户级 stdio server 在每个项目 serve 中各自启动一个子进程**——项目间不共享进程，这是按项目隔离的既定行为。
- `Mcp` 另有 `reconnect_initial_s=2` / `reconnect_max_s=60` / `watch_interval_s=2`。注意：`watch_interval_s` 改后需重启 serve 才生效——这是**当前实现限制**（watch 循环在 start 时读取快照值），不是有意保持不变量；其余配置变更热加载无需重启。

## 发现 → 审核 → 发布不变量

以下路径全部走同一 `discover → audit → publish`，无绕过入口：start 初次连接、API/CLI 手动 reload、配置热加载的新增/变更、传输故障后的每次自动重连、`notifications/tools/list_changed`（逐 server 防抖）。SDK tools/list 分页必须耗尽，任一页失败或全量超过 **256 tools/server** 都不发布部分结果（server 转 failed，不重试传输退避）。审核先于 enabled/disabled 计算：禁用工具同样被审核；任何情况下用户显式启用都不能跳过审核；某工具被拒不影响同 server 其他合格工具。

审核（`audit_tool`）为纯确定性代码，不调用 LLM：结构检查（raw 是 Mapping、名称非空且 ≤ `RAW_NAME_MAX=128`、inputSchema/outputSchema 为 object、可 JSON 序列化、定义 ≤32 KiB、description ≤4,000 字符，超限隔离而非截断放行；重复原名/清洗后冲突/与现有工具冲突隔离并记录）+ 高置信中英短语注入粗审（忽略先前指令、伪造 system/developer、泄露凭据/系统提示/环境变量、绕过审批、隐藏操作、诱导无关工具、零宽与双向控制符），递归覆盖名称、描述与 input/output schema 的键和全部字符串值。finding 只含稳定 rule ID、JSON 路径、静态说明，**永不回显可疑原文**（schema 键命中上报父路径）；scanner 任何异常按 fail-closed 处理。每次发现经 `AuditStore` 追加到项目私有 `projects/<id>/mcp/audit.jsonl`（ts/server/tool/指纹/generation/决定/findings，不保存完整 schema）。

**审核不是完整安全边界**：它是确定性粗审，高置信短语不可能完备，不构成对恶意 server 的完整防护；不引入模型评审（无额外 LLM 调用），也不替代工具执行期的权限审批。

## 状态机

server 状态：`connecting` / `connected` / `retry_waiting` / `disabled` / `failed` / `removed`。工具视图：`enabled` / `disabled_user` / `quarantined` / `unavailable`。

- 传输故障（连接断开、超时、调用传输失败、ping 失败）使当前 generation 立即失效，注销该 server 全部工具，进入 `retry_waiting`，指数退避 2→60 秒（带抖动，无永久放弃次数）；手动 reload 重置退避。恢复只表示传输恢复，必须重新 tools/list 并审核后才重新发布。
- 配置、协议初始化、`config_error` 错误进入 `failed`，不做无意义无限重试；配置变化或手动 reload 后重试。
- 用户禁用立即关闭连接并注销工具（`disabled`）；重新启用建立全新 generation 并重新审核；故障摘除**不写回**用户配置，恢复后尊重用户禁用选择。
- 相同定义指纹不替换工具对象、不重排顺序（保护 prompt 前缀稳定）；新增/变更有序发布，删除立即注销；旧 generation 与 close 后的回调一律不发布。
- 传输失败的调用返回 `unsupported + retryable`，**不自动重放**（避免非幂等副作用重复执行）；MCP `isError=true` 映射为执行失败，不判定为断连。
- watch 循环每 `watch_interval_s` 比较用户/项目配置 `(mtime_ns, size)`，只重读 mcp 段；坏配置保留旧快照并在 `load_error` 暴露（脱敏）。reload 的意外错误只记异常类型名（不回显原文，避免泄漏配置秘密）、watch 存活。

## 工具与执行链

- 通过审核且被用户选择的工具注册到共享 `FileToolRegistry`，`toolset="mcp"`（`BUILTIN_TOOLSETS` 中 default=True）；MCP 不覆盖内置/文件/其他 server 同名工具（冲突者隔离并记录）。`ToolRegistry` 已扩展 `unregister(name)`（`InMemoryToolRegistry` 返回被移除工具；`FileToolRegistry` 只注销 programmatic 工具，绝不删 TOOL.md）。
- 每个工具 `ToolSpec.defer` 继承 server `deferred`（默认 true），复用 `tool_search` 与 `revealed_tools`；没有 `tool_search` 的 agent 按现有规则看到全量 schema。registry 名是限定名，server 侧原始名记入 component metadata。
- annotations 保守映射：仅显式 `readOnlyHint=True` 视为只读；`destructiveHint` 显式声明按声明、未声明时按非只读保守处理；`openWorldHint` 缺省 true；`temporal` 不设置。`check_permission` 不额外开后门——manual/yolo/auto、plan mode 与会话批准仍由 `PermissionService` 决定。项目 override 决定项目可用，`AgentSpec.tools/disallowed_tools` 每次 run 继续取交集，委派沿用 `tool_ceiling`。
- 调用结果：文本块合并；dict `structuredContent` 写入 `structured_content`；图片块经 `FileMediaStore.save_bytes` 校验落盘为 `MediaInput`；embedded text resource 作为文本；resource link 只返回链接说明不主动抓取；音频/未知块返回明确未支持。`McpTool.execute` 只捕获 `McpTransportError`（client 层翻译断流/超时/httpx/anyio/OS 错误），协议错误与编程错误原样上抛。

## 管理 API / CLI / Web

`boot()` 构造 manager 放入 `BootResult.mcp`（只构造不连接）；serve 在 daemon 起之前 `await mcp.start()`，退出 finally 中 `await mcp.close()`。daemon（unix 与 TCP 两路同一实例）注册六个端点，受与现有路由相同的 Bearer/Origin/Unix socket 中间件保护：

- `GET /mcp/servers` — server/tool/用户选择/健康/退避时间/审核 finding 的脱敏视图 + 顶层 `load_error`。
- `POST /mcp/servers` — 注册项目 server（请求体经 `McpServer` 校验），写项目 config.json 并立即热连接（同状态机）。
- `DELETE /mcp/servers/{name}` — 删项目 server 定义与其项目 override（一次原子写）；用户级定义不隐式修改，同名用户 server 恢复可见；仅用户级存在的 name 是 200 幂等 no-op（不动用户配置）；merge 视图里完全不存在的 name 返回 404。
- `POST /mcp/servers/{name}/reload` — 强制重连、拉取、审核、发布（重置退避）。
- `PUT /mcp/servers/{name}/availability` — 字段级合并写项目 override（enabled/enabled_tools/disabled_tools/deferred），立即生效：请求未设置的字段保留原值，显式 `null` 清除该字段，显式 `[]` 仍是空选择；四个字段相互独立。
- `POST /mcp/reload` — 重读 mcp 配置，对变化项走同一状态机。

CLI：`xana mcp list`（server 状态/origin/transport/错误/retry_in、逐工具状态与脱敏 finding、`load_error`）与 `xana mcp reload [--server NAME]`，经现有 DaemonClient 调用运行中 serve，不在 CLI 侧第二次 boot。注册与细粒度选择在 Web 完成。

Web：工作台 MCP 面板（`web/src/McpPanel.vue`）是唯一浏览器入口；GET 投影是唯一权威视图，每个操作完成后统一重取；secret 字段（env/请求头值）`type="password"`、只从空表单提交、绝不回填服务器返回值。删除按钮仅对项目级 server 显示；隔离工具无启用 checkbox。

## 边界与限制（如实记录）

- **审核不是完整安全边界**（见上）；finding 只给规则/路径/静态说明。
- **http/sse 传输分支已实现但无真实 E2E**：全部真实端到端验证（连接、list_changed、kill 恢复、分页原子性）都在本地 stdio demo server 上完成；http/sse 只有配置/单元层覆盖。
- 端到端测试使用**显式 fake 模型**（配置 `type="fake"`）与真实本地 MCP stdio server（`tests/mcp_demo_server.py`）；未执行真实模型验收。
- 首版不支持 MCP OAuth、sampling、elicitation、roots、prompts/resources 管理；不自动重放断连调用；不抓取 resource link。
- `watch_interval_s` 变更需重启 serve 是当前实现限制，不是目标不变量。
- 部署：新代码需 `xana sync` 并重启正在运行的 serve；部署后 MCP 配置变更不再需重启。

## 架构验证

对照预置基线运行 `tools/check_arch_contracts.py`（基线取自 base 产品代码，输出分别存于
`/tmp/mcp-arch-baseline.txt` 与 `/tmp/mcp-arch-current.txt`），本模块新增 **16 条**启发式
旗标。检查器与公共契约**未被弱化或修改**，以下逐条如实列出；该偏差保持**开放**
（未宣称已解决，也未获用户接受），后续若需降噪应调整检查器对 JSON/HTTP 边界函数的
判定，而不是改代码绕过：

**naked dict return（9 条，全部是 JSON/HTTP 协议边界形状）：**

| 文件:行 | 符号 | 边界类别 |
| --- | --- | --- |
| `src/xana/cli/_daemon_client.py:106` | `mcp_servers() -> dict` | daemon HTTP JSON 客户端 |
| `src/xana/cli/_daemon_client.py:111` | `mcp_reload() -> dict` | 同上 |
| `src/xana/config/project_config.py:51` | `merge_mcp_configs() -> dict` | 配置合并(brief 规定签名) |
| `src/xana/mcp/config.py:33` | `expand_environment() -> dict[str, str]` | 凭据展开 |
| `src/xana/mcp/config.py:174` | `_read_mcp_section() -> dict` | 配置文件读取 |
| `src/xana/mcp/client.py:167` | `list_tools() -> tuple[dict, ...]` | MCP 原始协议 dict |
| `src/xana/runtime/mcp_http.py:22` | `_view_dict() -> dict` | JSON 投影 |
| `src/xana/runtime/mcp_http.py:26` | `_servers_payload() -> dict` | JSON 投影 |
| `src/xana/runtime/mcp_http.py:31` | `_json_object() -> dict` | aiohttp 请求体 |

**object type outside raw boundary（7 条）：**

| 文件:行 | 代码 | 类别 |
| --- | --- | --- |
| `src/xana/mcp/audit.py:156` | `value.get("type") == "object"` | JSON Schema 类型字面量 |
| `src/xana/mcp/tools.py:56` | `input_schema = {"type": "object"}` | JSON Schema 字面量 |
| `src/xana/mcp/contracts.py:44` | `object.__setattr__(...)`（`__post_init__`） | frozen dataclass 深层冻结惯用法 |
| `src/xana/runtime/mcp_http.py:31` | `_json_object` 请求体读取 | aiohttp JSON 边界 |
| `src/xana/runtime/mcp_http.py:37` | `raise web.HTTPBadRequest(...)` | 同上 |
| `src/xana/runtime/mcp_http.py:48` | `body = await _json_object(request)` | 同上 |
| `src/xana/runtime/mcp_http.py:90` | `body = await _json_object(request)` | 同上 |

这些类别与基线既有违例（HTTP/JSON 边界的 naked dict、aiohttp/JSON-Schema 的 object
字面量）一致，不构成实现依赖环、metadata 注入或第二执行路径。基线独有的
`src/xana/benchmarks/process.py:63` 旗标来自主仓库工作区中**未跟踪**的
`src/xana/benchmarks/` 目录（本分支 HEAD 不含），非本分支引入或修复。

## Owns

- MCP 配置快照与合并后的有效 server 视图、连接 generation 与退避重试、工具审核决定与 audit.jsonl、已发布 MCP 工具对象。

## Does Not Own

- 工具执行编排与权限决策（harness_sdk / policy）；配置模型定义（config/system_model.py，mcp 只做运行期读取/写入）；API 路由的鉴权中间件（daemon_http）。

## 测试

`tests/test_mcp_config.py`（合并/override/凭据展开/快照不可变）、`test_mcp_audit.py`（命名/结构/注入/fail-closed/指纹/AuditStore）、`test_mcp_tools.py`（unregister/annotations/权限/deferred/结果转换）、`test_mcp_manager.py`（状态机/热加载/退避/失效/选择保留）、`test_mcp_stdio.py`（4 个真实本地 stdio E2E）、`test_mcp_http.py`（API/CLI）、`test_mcp_boot.py`（boot/serve 接线）、`web/tests/workbench.spec.ts`（MCP 面板浏览器测试）。
