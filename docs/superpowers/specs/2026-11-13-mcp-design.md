---
title: MCP 连接、工具审核与热加载设计
status: active
date: 2026-11-13
---

# MCP 连接、工具审核与热加载设计

## 1. 意图

让 Xana 项目可以使用外部 MCP server 的工具，同时不把外部工具定义当作可信提示词。注册或修改配置后无需重启 serve；每次发现工具都先经过确定性粗审，再进入现有工具目录。连接故障只造成运行期摘除，不覆盖用户的启用选择。

本设计已落地（status: active），实现即本文描述的链，无第二套工具执行、权限或上下文披露机制；模块文档与已知限制见 [doc_ai/mcp](../../doc_ai/mcp.md)。

## 2. 目标与非目标

### 目标

- 支持 stdio、streamable HTTP（配置名 `http`）和 SSE 三类 MCP 连接，使用官方 Python `mcp` SDK。
- 用户级 `system.json` 与项目级 `projects/<id>/config.json` 都可声明 server；项目 serve 启动时自动连接。
- 注册、修改、删除、手动重载和外部配置修改均在运行中生效。
- 初次连接、手动刷新、自动重连和 `notifications/tools/list_changed` 后，都重新拉取并审核工具；未经本次审核的新定义不发布。
- 用户可按项目启停 server、选择工具；agent 继续通过 `AgentSpec.tools/disallowed_tools` 收窄可用范围。
- 传输故障立即摘除相关工具并自动重试；恢复后重新审核再发布。
- Web 工作台可查看状态/审核结果、注册项目 server、切换工具可用性和手动重载。
- 审核、故障和配置错误可观察，且不泄漏 env、headers 或工具定义中可能的秘密。

### 非目标

- 首版不实现 MCP OAuth、sampling、elicitation、roots、prompts 或 resources 管理。
- 不把粗审描述成完整安全保证；不引入模型评审工具定义的额外 LLM 调用。
- 不增加按飞书/Telegram 最终用户分别授权的工具 ACL。这里的“用户选择”是项目操作者通过受认证的工作台/管理接口做出的项目级选择。
- 不为 MCP 工具建立独立权限链、独立 `tool_search` 或第二套 agent 工具白名单。
- 不自动重放因断连而失败的外部调用，避免非幂等副作用重复执行。
- 不自动抓取 MCP 返回的 resource link，避免把工具结果变成隐式网络请求。

## 3. 反目标与约束

- 配置显示已注册但从未连接、审核失败仍暴露 schema、断连后旧工具继续可调用，都视为失败。
- 不因 reload 的相同结果反复替换工具对象或重排工具顺序，避免无意义地破坏 prompt 前缀。
- 不把故障禁用写回用户配置，不用 metadata 隐藏注入依赖，不新增常驻兼容层或 `v2` 路径。
- `boot()` 仍是唯一组合点；长生命周期任务由 serve 显式启动和关闭。
- 不新增 ABC。连接、配置和审核使用具体类与构造注入的工厂/时钟；只扩展现有 `ToolRegistry` 契约以支持注销。
- 新增或修改模型可见提示词不是本设计的一部分；审核规则是确定性代码，不创作额外模型指令。

## 4. 方案比较与选择

### A. 官方 SDK + 现有 ToolRegistry（已选择）

引入官方 `mcp` 依赖，以 `McpManager` 管理配置、连接、审核和工具发布。优点是协议细节由 SDK 承担，工具复用现有 schema、dispatch、权限、延迟披露和持久历史。代价是新增一个正式运行时依赖和一块连接生命周期代码。

### B. 手写 stdio JSON-RPC

可减少依赖，但初始化、分页、通知、取消、超时和远程传输都需自行维护。安全性和长期维护成本高于收益，排除。

### C. Extension ToolContributor

当前 extension 工具在 run 开始时装箱、始终全量披露，且不能自然参与现有 deferred 机制。用它会造成不同的热更新与披露语义，排除。

## 5. 配置契约

### 5.1 server 定义

扩展现有 `McpServer`，保留 `name` 与 `transport`，增加以下已类型化字段；未知字段暂不静默丢弃，配置加载时明确报错，避免拼写错误造成错误连接。

- 通用：`enabled: bool = True`、`deferred: bool = True`、`startup_timeout_s: float = 15`、`tool_timeout_s: float = 60`。
- stdio：`command: str`、`args: list[str]`、`env: dict[str,str]`、`cwd: str | None`。未写 cwd 时使用项目 workspace。
- http/sse：`url: str`、`headers: dict[str,str]`。
- `transport` 为 `stdio | http | sse`；按 transport 校验必填字段和互斥字段。

`Mcp` 增加 `overrides: dict[str, McpServerOverride]` 与重连/轮询默认值。override 只包含 `enabled`、`enabled_tools`、`disabled_tools`、`deferred`，不能修改命令、URL 或凭据。`enabled_tools=None` 表示全部，`[]` 表示全不选；随后再减去 `disabled_tools`。

### 5.2 分层合并

`PROJECT_KEYS` 增加 `mcp`。`merge_system_config` 保持现有顶层覆盖规则，但对 mcp 使用专门合并函数：

1. 用户和项目的 `servers[]` 分别按 name 索引；项目同名 server 整体替换用户连接定义，新 name 追加。
2. 用户与项目的 `overrides` 按 name 合并，项目字段优先。
3. override 应用到合并后的同名 server；无对应 server 的 override 保留在配置中但状态显示为未匹配，不隐式创建空连接。
4. `tools.disabled` 继续使用现有全局并集规则，对 MCP 组名和工具全名同样生效。

分层加载与运行期重载共用 `config/project_config.py` 中的同一个 mcp 合并函数。运行期只重读用户/项目文件的 mcp 段，不改变其他配置 read-once 语义；`SystemConfig` 的 boot 快照不被原地修改。

### 5.3 凭据引用

首版支持 `${ENV_NAME}` 形式从 serve 进程环境展开 env/header 值。未设置的变量、遗留 `${secret:...}` 或其他无法解析的引用均使该 server 进入配置失败并给出字段位置，绝不把占位符原文发给子进程或远端。首版不另建 secret store；API 返回配置时只返回脱敏结构，不回显 env/header 值。

## 6. 模块与所有权

新增 `src/xana/mcp/`：

- `contracts.py`：不可变运行状态、审核发现、工具/服务器视图；不重复定义 Pydantic 配置模型。
- `audit.py`：纯确定性工具定义结构检查、注入特征扫描和摘要。输入原始 tool 定义，输出逐工具决定与 finding。
- `naming.py`：`mcp__{server}__{tool}` 名称清洗、64 字符上限和稳定短 hash；规则与 Kimi `mcpCore/tool-naming.ts` 对齐。
- `tools.py`：现有 `Tool` ABC 的 MCP 实现、MCP annotations 到 `ToolSpec` 的映射、调用结果到 `ToolResult` 的转换。
- `client.py`：官方 SDK 的 stdio/http/sse 连接适配。每个连接拥有独立 `AsyncExitStack`；只暴露 manager 需要的具体连接对象，不新增公共 ABC。
- `manager.py`：唯一连接与发布状态机，持有配置快照、连接 generation、逐 server 锁、重试任务和当前已审核工具快照。
- `config.py`：运行期 mcp 配置读取、原子项目写入、文件版本轮询与手动 reload；复用配置模块的合并函数。
- `audit_store.py`：向项目私有 `mcp/audit.jsonl` 追加审核结果，记录时间、server/tool、定义 SHA-256、generation、决定、规则 ID 和 JSON 路径，不保存完整 schema 或可疑原文。
- `runtime/mcp_http.py`：daemon 内的管理/状态 API；不是独立服务。
- `mcp/__init__.py`：只导出必要入口，不隐藏全局单例。

`boot()` 构造 manager 并放入 `BootResult.mcp`；serve 启动时 `await mcp.start()`，关闭时 `await mcp.close()`。start 并发执行首次连接，单次启动受 server timeout 限制；后续 watch/retry 不阻塞主对话。

## 7. 工具发布与现有执行链

### 7.1 注册与选择

- 通过审核且被用户选择的工具注册到共享 `FileToolRegistry`，`toolset="mcp"`；`BUILTIN_TOOLSETS` 增加 default=True 的 mcp 组。
- 扩展 `ToolRegistry` 增加 `unregister(name)`，同步实现 `InMemoryToolRegistry`、`FileToolRegistry` 和测试 fake。MCP 不覆盖内置、文件或其他 server 的同名工具；冲突者隔离并记录。
- 每个工具 `ToolSpec.defer` 继承 server 设置，默认 true，复用 `tool_search` 和 `revealed_tools`。没有 `tool_search` 的 agent 按现有规则看到全量 schema。
- 项目 override 决定项目是否可用，`AgentSpec.tools/disallowed_tools` 在每次 run 继续取交集；委派沿用 `tool_ceiling`，不能因子 agent 配置扩大权限。
- 工具描述和 schema 来自已审核快照。相同定义 hash 不替换对象；新增/变更有序发布，删除立即注销，保持确定性顺序。

### 7.2 权限与副作用

MCP `readOnlyHint`、`destructiveHint`、`openWorldHint` 映射到现有 `ToolSpec`：只有明确声明 readOnly 才视为只读；未声明 destructive 时按非只读保守处理；openWorld 默认 true。`check_permission` 不额外开后门，仍由 `PermissionService` 应用 manual/yolo/auto、plan mode 和会话批准。工具不可用或所属 agent 不匹配时，schema 与执行都拒绝。

### 7.3 调用结果

- 文本 content 合并为模型结果；`structuredContent` 写入 `ToolResult.structured_content`，不与文本重复时保留。
- 图片 blob 经现有 `FileMediaStore.save_bytes` 校验并保存为 `MediaInput`，沿用大小和格式限制。
- embedded text resource 作为文本；resource link 仅返回链接说明，不主动访问。
- 首版不支持音频和未知二进制块，返回明确的未支持说明，不伪装成功交付。
- MCP `isError=true` 映射为工具执行失败，不据此判定服务器断连。
- 传输异常使该 server 当前 generation 失效并摘除工具，本次调用返回 `unsupported + retryable`，不在新连接上自动重放。

## 8. 每次发现的审核不变量

### 8.1 触发点

以下路径全部调用同一个 `discover -> audit -> publish`，没有绕过入口：

- start 初次连接；
- API/CLI 手动 reload；
- 配置热加载后的新增或变更连接；
- 传输故障后的每次自动重连；
- server 的 `notifications/tools/list_changed`（逐 server 防抖）；
- 工具调用后发现连接 generation 已变化时，不继续使用旧定义发布任何内容。

SDK 的 tools/list 分页必须耗尽。拉取失败不拿部分列表发布。

### 8.2 结构与资源检查

逐工具检查：名称非空且长度有界；inputSchema 必须是 object 类型 JSON object；outputSchema 若存在也须为 object；原始定义可 JSON 序列化。设置初始上限：256 tools/server、单工具定义 32 KiB、描述 4,000 字符；超限工具隔离而不是截断后放行。重复原始工具名、清洗后名称冲突、与现有工具冲突均记录并隔离。

### 8.3 提示词注入粗审

递归扫描名称、描述、input/output schema 的键和所有字符串值，包括 title、description、default、examples、enum 与嵌套 properties。首版规则覆盖：

- 忽略/覆盖 system、developer、先前指令的典型中英表述；
- 伪造 system/developer 或特殊对话控制标签；
- 要求泄露系统提示词、凭据、环境变量、本地文件或向外部地址回传；
- 要求隐藏操作、绕过批准/安全策略，或操纵 agent 调用无关工具；
- 零宽字符、双向控制符等常用于隐藏内容的 Unicode 特征。

命中即隔离该工具，finding 包含稳定 rule ID 与 JSON 路径。正常工具说明可能讨论“系统”“权限”等词，因此使用高置信短语而非单词封禁；测试同时覆盖恶意样本与合理误报样本。扫描器异常按失败关闭处理，不发布未审核定义。

### 8.4 审核与用户选择的关系

审核先于 enabled/disabled 计算；禁用工具也可记录审核结果，但任何情况下都不能因用户显式启用而跳过审核。某工具被拒绝不影响同 server 其他合格工具。审核失败状态与连接状态分别显示，避免“进程在线”被误读为“工具可用”。

## 9. 生命周期与故障处理

服务器公开状态至少包括：`connecting`、`connected`、`retry_waiting`、`disabled`、`failed`、`removed`；工具另有 `enabled`、`disabled_user`、`quarantined`、`unavailable` 视图。

- 连接或工具调用出现传输故障：当前 generation 立即失效，注销该 server 全部工具，进入 retry_waiting。
- 传输故障使用指数退避和抖动，初始 2 秒、上限 60 秒，不设置永久放弃次数；手动 reload 重置退避。
- 配置、协议初始化、凭据展开错误进入 failed，不做无意义无限重试；配置变化或手动 reload 后重试。
- 用户禁用立即关闭连接并注销工具；重新启用建立全新 generation 并重新审核。
- 自动重连成功只表示传输恢复，必须完成 tools/list 和审核后才重新注册。
- close 取消 watch、retry、通知刷新与在途连接，逐个关闭 SDK session/子进程；旧 generation 的完成回调不得重新发布工具。

运行状态以 manager 为权威，audit.jsonl 为只追加审计记录；不写回 system.json 伪造健康状态。

## 10. 热加载与管理 API

配置服务每 2 秒比较用户/项目配置文件的 `(mtime_ns, size)`，只针对 mcp 触发重读；管理写入后调用同一 reload，不依赖轮询及时性。坏 JSON/坏 schema 保留上一份可用连接快照并报告错误，不删除正在使用的 server。

在现有 daemon 注册以下受相同 Bearer/Origin/Unix socket 保护的端点：

- `GET /mcp/servers`：服务器、工具、用户选择、健康、重试时间与审核 finding 的脱敏视图。
- `POST /mcp/servers`：注册项目 server；请求体为已校验配置，不执行请求中额外脚本。
- `DELETE /mcp/servers/{name}`：删除项目 server/override；用户级定义不由此隐式修改。
- `POST /mcp/servers/{name}/reload`：强制重连、拉取、审核和发布。
- `PUT /mcp/servers/{name}/availability`：字段级合并写项目 override（enabled、enabled_tools、disabled_tools、deferred），立即生效：请求未设置的字段保留项目层原值，显式 `null` 清除该字段回落到用户层/server 默认语义，显式 `[]` 仍是空选择。
- `POST /mcp/reload`：重读 mcp 配置并对变化项执行同一状态机。

CLI 首版只提供 `xana mcp list` 与 `xana mcp reload` 作为 daemon 管理客户端；注册和细粒度选择在 Web 完成，避免再维护一套交互式配置向导。

## 11. Web 工作台

在现有工作台增加一个 MCP 面板，不新建前端应用：服务器状态/错误/下次重试、工具审核状态、启用 checkbox、重新加载和项目 server 注册/删除。工具列表来自 GET 投影，操作后重新获取权威状态，不在浏览器维护第二份配置。

注册表单按 transport 显示 command/args/env 或 url/headers。秘密字段提交后不再次显示；列表只显示是否已配置。前端继续经过 `WorkbenchClient`，复用 token、错误展示与构建产物流程。

## 12. 测试与验收

### 单元/契约

- 配置：用户/项目合并、override、未知字段、transport 校验、环境展开和占位符失败、坏配置保留旧快照。
- 审核：嵌套 schema 注入、控制字符、误报样本、超限、重复/冲突、scanner fail-closed；证明每次 discovery 都调用审核。
- registry：注册/注销、不覆盖内置、相同 hash 不替换、工具顺序稳定。
- manager：启动自动连接、增改删热加载、list_changed、退避重试、故障摘除、恢复重审、用户禁用不被恢复覆盖、旧 generation 回调失效、close 清理。
- 工具：annotations、三档权限与 plan mode、agent/委派交集、文本/结构化/图片结果、MCP 错误、断连不自动重放。
- HTTP：认证、脱敏、项目注册/删除、availability、手动 reload。

### 真实端到端

使用官方 SDK 编写测试专用本地 stdio server，不使用外部服务：

1. 经 API 注册，serve 自动初始化并发现工具；fake model 经唯一 harness 调用 `mcp__demo__echo` 并获得真实结果。
2. server 增加一个正常工具和一个含注入定义的工具并发 list_changed；正常工具经 tool_search 后可调用，恶意工具始终无 schema 且不可执行，审计记录包含两次发现。
3. 杀死 server，工具从目录消失；重启后自动重连、重新审核并恢复，期间调用不重复执行。
4. Web 取消工具勾选后，下一条 run 不可见/不可调用；刷新页面和服务重启后选择仍保留，重连不会重新打开它。
5. 外部编辑项目 config.json，无需重启 serve 完成新增、修改和删除。
6. 跑现有 canonical fake pipeline、deferred/tool_search、权限、agent delegation 和 Web 测试，确保无第二执行路径。

全量 `uv run pytest` 必须全绿、零新增测试失败/收集错误；Web 运行 `npm ci --ignore-scripts`、`npm run build`、`npm test`。真实模型验收默认 Kimi 2.8，只有其已配置可用时才执行，不自动换 K3。

**架构检查验收说明（实际结果，如实记录）**：本设计原本要求 `tools/check_arch_contracts.py` 零新增违例，该预期**未达成**。实际验收（基线对照，见 Task 7 报告与 [doc_ai/mcp.md](../doc_ai/mcp.md)「架构验证」）新增 16 条启发式旗标，全部是 JSON/HTTP 协议边界的 naked-dict 返回与 object 字面量类别，与基线既有违例同类；检查器与公共契约未被弱化以换取绿色输出。逐文件清单与分类见 [doc_ai/mcp.md](../doc_ai/mcp.md)，该偏差保持开放，不得从本文措辞推断为完全合规。

## 13. 文档与交付边界

完成后新增 `doc_ai/mcp.md`，更新 config merge、tools registry/lifecycle、runtime boot/lifecycle、workbench、CLI 和模块导航；记录审核不是完整安全边界、OAuth/资源等非目标。旧 `Mcp/McpServer` 外壳被完整实现替代，不保留另一套管理入口。

交付后提醒执行 `xana sync` 并重启正在运行的 serve 以部署新代码；部署后的 MCP 配置变更不再需要重启。
