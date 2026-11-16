# MCP Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 Xana 项目提供可热加载、每次发现均审核、可选择工具且故障自动恢复的 MCP 工具能力。

**Architecture:** 官方 Python MCP SDK 负责协议，`McpManager` 统一配置、连接 generation、审核、重试与工具发布；MCP 工具进入现有 ToolRegistry/harness/权限链。daemon 与现有 Vue 工作台只提供同一 manager 的管理投影，不建立第二执行路径。

**Tech Stack:** Python 3.10+、Pydantic 2、asyncio、aiohttp、官方 `mcp` 1.x SDK、pytest/pytest-asyncio、Vue 3、TypeScript、Playwright。

**Spec:** `docs/superpowers/specs/2026-11-13-mcp-design.md`

## Global Constraints

- 初次连接、reload、自动重连和 `notifications/tools/list_changed` 必须走同一个 `discover -> audit -> publish`；未审核定义不得发布。
- 工具名使用 `mcp__{server}__{tool}`，最长 64 字符，稳定清洗/hash；默认 deferred，复用 `tool_search`。
- 用户禁用、运行故障、审核隔离分开；重连不得覆盖项目 override。
- 传输故障立即摘除工具并指数退避（2 秒至 60 秒）；失败调用不自动重放。
- `boot()` 唯一组合点，serve 拥有 start/close；不新增 ABC，不用 metadata 隐藏注入，不建立 v2/legacy 路径。
- 新依赖仅正式运行时 `mcp>=1,<2` 及 lockfile；测试使用项目已有 pytest/Playwright。
- 不自动 git commit；每个任务完成后保留可审查 diff 并运行所列验证。
- 模型可见提示词不在本计划中新增或修改。

## Review Focus

- 分页 tools/list 后段含恶意工具或拉取中断：不得发布部分列表；Task 4 的 `test_discovery_is_atomic_and_audits_every_page`。
- 嵌套 schema、Unicode 隐藏控制符与正常安全说明：高置信隔离且不无差别封禁；Task 2 的审核参数化测试。
- 断连发生在有副作用调用之后：本次失败可重试提示但不自动重放；Task 3/4 的断连测试。
- reload/close 与旧连接回调竞态：旧 generation 不得重新上架；Task 4 的 stale generation 测试。
- API/审计中的 env、headers、schema 示例秘密：不得回显或落盘；Task 1/5 的脱敏测试。

---

### Task 1: MCP 配置模型、分层合并与凭据展开

**Files:**
- Modify: `src/xana/config/system_model.py:118-129`
- Modify: `src/xana/config/project_config.py:18-62`
- Modify: `src/xana/config/loader.py:84-122`
- Create: `src/xana/mcp/__init__.py`
- Create: `src/xana/mcp/contracts.py`
- Create: `src/xana/mcp/config.py`
- Test: `tests/test_mcp_config.py`

**Interfaces:**
- Produces: `McpServerOverride(enabled=None, enabled_tools=None, disabled_tools=None, deferred=None)`。
- Produces: `merge_mcp_configs(user: dict, project: dict) -> dict`，供 boot 与运行期 loader 共用。
- Produces: `ConfiguredServer(server: McpServer, origin: Literal["user","project"], enabled: bool, enabled_tools: frozenset[str] | None, disabled_tools: frozenset[str], deferred: bool)`。
- Produces: `McpConfigSnapshot(servers: tuple[ConfiguredServer, ...], reconnect_initial_s: float, reconnect_max_s: float, watch_interval_s: float)`。
- Produces: `McpConfigStore.load() -> McpConfigSnapshot`、`write_project_server(McpServer)`、`delete_project_server(str)`、`write_project_override(str, McpServerOverride)`、`versions() -> tuple[tuple[int,int], tuple[int,int]]`。
- Produces: `DefaultConfigProvider.system_config_path -> Path`，供 boot 构造 store。

- [ ] **Step 0: 在修改产品代码前建立全量基线**

Run: `uv run pytest > /tmp/mcp-main-baseline.txt 2>&1`
Expected: 命令完成并记录当前 main 的预置失败/收集错误；后续只比较零新增回归，不假定历史摘要仍准确。

- [ ] **Step 1: 写配置失败测试**

覆盖：三种 transport 必填/互斥字段；未知字段报错；用户/项目 server 同名替换和 override 优先；`enabled_tools=[]`；`${ENV_NAME}` 展开；未设置变量与 `${secret:...}` 明确失败；坏项目 JSON 不覆盖旧快照。

```python
assert effective.enabled_tools == frozenset()
with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
    expand_environment({"MINIMAX_API_KEY": "${MINIMAX_API_KEY}"}, environ={})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_mcp_config.py -q`
Expected: 因新字段/类不存在而 FAIL。

- [ ] **Step 3: 实现配置与 store**

`McpServer` 使用 `Literal["stdio","http","sse"]` 和 model validator；`Mcp` 增加 `overrides`、`reconnect_initial_s=2`、`reconnect_max_s=60`、`watch_interval_s=2`。`PROJECT_KEYS` 加 `mcp`，merge 对 server 按名、override 按字段分层。store 通过 `update_project_config` 原子写项目配置，load 返回不可变 snapshot 与 origin。

- [ ] **Step 4: 运行配置与既有配置测试**

Run: `uv run pytest tests/test_mcp_config.py tests/test_config_system_model.py tests/test_config_loader.py tests/test_projects.py tests/test_review_batch1_fixes.py -q`
Expected: PASS。

### Task 2: 工具命名与确定性安全粗审

**Files:**
- Create: `src/xana/mcp/naming.py`
- Create: `src/xana/mcp/audit.py`
- Create: `src/xana/mcp/audit_store.py`
- Test: `tests/test_mcp_audit.py`

**Interfaces:**
- Consumes: Task 1 的 dataclass-only contracts 约束。
- Produces: `qualified_tool_name(server_name: str, tool_name: str) -> str`。
- Produces: `audit_tool(raw: Mapping[str, Any]) -> tuple[AuditFinding, ...]`；空 tuple 表示通过。
- Produces: `tool_fingerprint(raw: Mapping[str, Any]) -> str`（规范化 JSON 的 SHA-256）。
- Produces: `AuditStore.record(server, tool, fingerprint, generation, findings) -> None`，仅追加脱敏 JSONL。

- [ ] **Step 1: 写审核与命名失败测试**

参数化覆盖忽略先前指令、伪造 system 标签、泄露凭据/系统提示词、绕过审批、隐藏操作、零宽/bidi；恶意文本放入 `inputSchema.properties.x.description` 和 outputSchema，证明递归扫描。正常“返回权限错误”“读取系统时间”不封禁。另测单工具 32 KiB、描述 4,000 字符边界；256 tools/server、分页原子性、重复名/冲突由 manager 测试接手。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_mcp_audit.py -q`
Expected: 模块不存在，FAIL。

- [ ] **Step 3: 实现命名、审核与脱敏审计**

命名规则对齐 Kimi：非 `[A-Za-z0-9_-]` 转 `_`、压缩下划线、超长追加稳定 8 位 hash。审核只返回 rule ID、JSON path、简短说明；AuditStore 不保存原始描述/schema。结构非法与扫描器异常均产生拒绝结果，不抛出后放行。

- [ ] **Step 4: 运行审核测试**

Run: `uv run pytest tests/test_mcp_audit.py -q`
Expected: PASS。

### Task 3: Registry 注销与 MCP Tool 适配

**Files:**
- Modify: `src/xana/tools/interfaces.py:45-59`
- Modify: `src/xana/tools/registry.py`
- Modify: `src/xana/tools/file_registry.py:35-68`
- Modify: `src/xana/tools/toolsets.py:18-34`
- Modify: `tests/test_fake_pipeline_flow.py:480-502`（只增加契约方法，不改 canonical 行为）
- Create: `src/xana/mcp/tools.py`
- Test: `tests/test_mcp_tools.py`
- Test: `tests/test_in_memory_tool_registry.py`

**Interfaces:**
- Produces: `ToolRegistry.unregister(name: str) -> Tool | None`；只注销 programmatic registered 工具，未知名/文件工具返回 None。
- Produces: `McpCallResult(content: tuple[dict[str,Any], ...], structured_content: Any, is_error: bool)`，加入 `mcp/contracts.py`。
- Produces: `build_tool_spec(config: ConfiguredServer, raw: Mapping, qualified_name: str) -> ToolSpec`。
- Produces: `McpTool(spec, connection, on_transport_failure, is_current, media_store)`，实现现有 Tool ABC。
- Produces: `convert_mcp_result(result: McpCallResult, media_store: FileMediaStore) -> ToolResult`。

- [ ] **Step 1: 写 registry、权限、结果与断连失败测试**

覆盖：unregister 不删 TOOL.md；annotations 保守映射；manual/plan mode 经现有 PermissionService；文本/structuredContent/图片；resource link 不主动 fetch；音频/未知 blob 明确未支持；`isError` 不摘除 server；传输错误返回 `failure_kind="unsupported"`、`retryable=True` 且调用次数恰为 1。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_mcp_tools.py tests/test_in_memory_tool_registry.py -q`
Expected: unregister/McpTool 不存在，FAIL。

- [ ] **Step 3: 实现 registry 与工具适配**

新增 default=True 的 `mcp` Toolset。McpTool 执行前检查 generation/启用状态；`check_permission` 只声明 allow，实际策略仍由 PermissionService。图片使用现有 FileMediaStore；不添加额外网络抓取或自动重试。

- [ ] **Step 4: 运行工具与 canonical harness 测试**

Run: `uv run pytest tests/test_mcp_tools.py tests/test_in_memory_tool_registry.py tests/test_fake_pipeline_flow.py tests/test_tool_search.py tests/test_deferred_tools.py tests/test_workbench_permissions.py -q`
Expected: PASS。

### Task 4: SDK 连接、审核发布状态机与热重载

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/xana/mcp/client.py`
- Create: `src/xana/mcp/manager.py`
- Create: `tests/mcp_demo_server.py`
- Test: `tests/test_mcp_manager.py`
- Test: `tests/test_mcp_stdio.py`

**Interfaces:**
- Consumes: Task 1 store/snapshot、Task 2 audit/naming、Task 3 registry/tool。
- Produces: `McpClientConnection.connect(config: ConfiguredServer, *, environ: Mapping[str,str], on_tools_changed: Callable[[],None], on_closed: Callable[[BaseException | None],None]) -> tuple[McpClientConnection, tuple[dict[str,Any], ...]]`。
- Produces: `McpClientConnection.list_tools() -> tuple[dict[str,Any], ...]`、`call_tool(name: str, arguments: dict, timeout_s: float) -> McpCallResult`、`ping() -> None`、`close() -> None`；内部持有 SDK AsyncExitStack。
- Produces: `McpManager(store, registry, media_store, *, connector=McpClientConnection.connect, auditor=audit_tool, environ=None, sleeper=asyncio.sleep, health_interval_s=30)`。
- Produces: `start()/close() -> None`、`reload(name: str | None = None) -> tuple[ServerView, ...]`、`register_server(config: McpServer) -> ServerView`、`remove_server(name: str) -> None`、`set_availability(name: str, override: McpServerOverride) -> ServerView`、`list() -> tuple[ServerView, ...]`。
- Produces: `ServerView`/`ToolView` 脱敏状态，供 Task 5 API 使用。

- [ ] **Step 1: 添加官方 SDK 并确认导入**

Run: `uv add 'mcp>=1,<2'`
Expected: pyproject/uv.lock 更新，`uv run python -c "import mcp; from mcp.client.stdio import stdio_client"` 成功。

- [ ] **Step 2: 写状态机失败测试**

使用注入 connector/auditor/fake sleep，覆盖：启动并发连接；配置增改删；每次 list_changed/retry 都审核；`test_discovery_is_atomic_and_audits_every_page`；恶意工具不注册；传输故障摘除并 2→60 秒重试；恢复尊重用户 disabled；config/protocol 错误不无限重试；stale generation 回调和 close 后回调不发布。

- [ ] **Step 3: 运行状态机测试确认失败**

Run: `uv run pytest tests/test_mcp_manager.py -q`
Expected: manager/client 不存在，FAIL。

- [ ] **Step 4: 实现 client 与 manager**

client 支持 stdio/http/sse、tools/list 分页、list_changed 回调、ping、timeout 和完整关闭。manager 先完整拉取/审核，再在同一事件循环临界区注销/注册差异；健康检查失败与调用传输失败共用失效入口。轮询配置 `(mtime_ns,size)`，坏配置保留旧快照。

- [ ] **Step 5: 写并运行真实 stdio 端到端测试**

`tests/mcp_demo_server.py` 使用官方 SDK 提供 echo、动态工具变更与恶意定义。测试真实 initialize/list/call；list_changed 后正常工具上架/恶意工具隔离；kill 后工具消失，重启自动恢复；每次恢复都产生新审核记录。

Run: `uv run pytest tests/test_mcp_manager.py tests/test_mcp_stdio.py -q`
Expected: PASS，无外部网络服务。

### Task 5: boot/serve 生命周期、daemon API 与轻量 CLI

**Files:**
- Modify: `src/xana/runtime/boot.py:160-192,226-242,687-726`
- Modify: `src/xana/cli/serve.py:157-181,198-351`
- Modify: `src/xana/runtime/daemon_http.py:38-53,203-233`
- Create: `src/xana/runtime/mcp_http.py`
- Create: `src/xana/cli/commands/mcp.py`
- Modify: `src/xana/cli/xana.py`
- Modify: `web/tests/server.py`
- Test: `tests/test_mcp_http.py`
- Test: `tests/test_mcp_boot.py`

**Interfaces:**
- Produces: `BootResult.mcp: McpManager`；boot 只构造，serve start/close。
- Produces: 设计 §10 的 GET/POST/DELETE/PUT `/mcp*` 路由。
- Produces: `xana mcp list`、`xana mcp reload [--server NAME]`，经现有 daemon client 调用。

- [ ] **Step 1: 写 API 与生命周期失败测试**

覆盖认证；注册后无需重启连接；availability 立即改变工具目录；DELETE 只处理项目层；reload 触发新审核；GET 不含 env/header 值；serve 关闭取消 watch/retry/SDK；CLI 参数映射正确。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_mcp_http.py tests/test_mcp_boot.py -q`
Expected: 路由/CLI/BootResult 字段不存在，FAIL。

- [ ] **Step 3: 接线并实现 API/CLI**

boot 用 `cp.system_config_path`、`cp.project_config_path`、ToolRegistry、FileMediaStore 构造 manager；daemon 两路 app 均注册同一 manager。serve 启动 manager 后开放主对话，finally 先停止接受新操作再 close。API 请求由 Pydantic 配置模型校验，异常映射 400/404/409，不返回秘密。

- [ ] **Step 4: 运行服务相关回归**

Run: `uv run pytest tests/test_mcp_http.py tests/test_mcp_boot.py tests/test_mcp_stdio.py tests/test_serve_config.py tests/test_serve_daemon_integration.py tests/test_daemon_http_basic.py tests/test_daemon_auth.py tests/test_daemon_client.py -q`
Expected: PASS。

### Task 6: Web MCP 管理面板

**Files:**
- Create: `web/src/McpPanel.vue`
- Modify: `web/src/App.vue`
- Modify: `web/src/client.ts`
- Modify: `web/src/contracts.ts`
- Modify: `web/src/style.css`
- Modify: `web/tests/workbench.spec.ts`
- Modify: `web/tests/server.py`
- Modify: `src/xana/workbench/static/index.html` 与构建 assets（由 npm build 生成）

**Interfaces:**
- Consumes: Task 5 `/mcp*` API。
- Produces: 现有工作台内 MCP 面板；不新增路由应用或前端配置权威。

- [ ] **Step 1: 写浏览器失败测试**

在隔离 fixture 注册本地 demo server。Playwright 验证状态/工具显示、勾选后 GET 与项目配置一致、刷新仍保留、手动 reload、审核 finding 可见、注册/删除项目 server、秘密字段不回显。

- [ ] **Step 2: 运行浏览器测试确认失败**

Run: `cd web && npm test -- --grep MCP`
Expected: MCP 入口不存在，FAIL。

- [ ] **Step 3: 实现面板并接入现有 App**

McpPanel 只调用 WorkbenchClient 并在操作后刷新权威视图；表单按 transport 显示字段，secret input 使用 password 且不以服务器返回值回填。复用现有 detail pane、按钮、错误提示样式。

- [ ] **Step 4: 构建并运行 Web 验证**

Run: `cd web && npm ci --ignore-scripts && npm run build && npm test`
Expected: TypeScript/Vite/Playwright 全部 PASS，构建产物写入 Python 包现有 static 路径。

### Task 7: 文档、架构检查与全量验收

**Files:**
- Create: `doc_ai/mcp.md`
- Modify: `doc_ai/README_ai.md`
- Modify: `doc_ai/module_io_map.md`
- Modify: `doc_ai/config_merge.md`
- Modify: `doc_ai/tools_registry.md`
- Modify: `doc_ai/tools_lifecycle.md`
- Modify: `doc_ai/runtime_boot.md`
- Modify: `doc_ai/runtime_lifecycle.md`
- Modify: `doc_ai/workbench.md`
- Modify: `doc_ai/cli_serve.md`
- Modify: `doc_ai/cli_xanac.md`（仅在确有 MCP 客户端命令变化时）
- Modify: `docs/superpowers/specs/2026-11-13-mcp-design.md`（status: active）

- [ ] **Step 1: 更新文档到实际实现**

记录配置示例、审核边界、状态机、API、每项目用户级 stdio 子进程会各自启动、非目标、测试证据和 sync/restart 部署方式；不得保留“只有配置外壳”的旧描述。

- [ ] **Step 2: 运行 MCP 与关键共享链测试**

Run: `uv run pytest tests/test_mcp_config.py tests/test_mcp_audit.py tests/test_mcp_tools.py tests/test_mcp_manager.py tests/test_mcp_stdio.py tests/test_mcp_http.py tests/test_mcp_boot.py tests/test_fake_pipeline_flow.py tests/test_tool_search.py tests/test_deferred_tools.py tests/test_agent_delegation.py tests/test_workbench_permissions.py -q`
Expected: PASS。

- [ ] **Step 3: 对照预置基线证明零新增失败**

Run: `uv run pytest > /tmp/mcp-all.txt 2>&1`
Expected: 与 Task 1 保存的 `/tmp/mcp-main-baseline.txt` 相比零新增失败、零新增收集错误；所有新增 MCP 测试 PASS。

- [ ] **Step 4: 架构与前端最终检查**

Run: `uv run python tools/check_arch_contracts.py`
Run: `cd web && npm run build && npm test`
Expected: 零新增架构违例；前端 PASS。

- [ ] **Step 5: 检查最终交付**

Run: `git status --short && git diff --stat && git diff --check`
Expected: 无空白错误、无临时/secret 文件、无重复 v2 接口。若真实模型验收需要执行，先确认 Kimi 2.8 已配置可用；不可用则明确记录未执行，不自动换 K3。最终提醒用户 `xana sync` 并重启 serve 部署，之后 MCP 配置变更加载不再需重启。
