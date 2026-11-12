---
title: harness compact
type: doc
status: active
---

# 上下文压缩

当前完整契约见 [会话、插话与取消](../docs/design/session-and-card-state.md)。

自动压缩和 `/compact` 共用 `Harness.compact` 的请求准备、`harness_sdk/compact.py` 的摘要生成，
上下文冻结、验证、存盘与替换统一归 `ContextSDK.compact`。
冻结 SDK 前缀和主日志行 ID，生成九节摘要，先持久保存完整转录，再替换主日志前缀和 SDK。
取消生成、磁盘写入失败或前缀不匹配时保持原消息；提交开始后 shield 并等待提交完毕。

手动压缩通过 TurnCoordinator.maintenance 排到完整响应/工具批次边界；空闲压缩独占 sid。
手动取消有独立 AbortSignal，取消压缩不会取消回答。自动压缩使用当前 run 的取消信号。
摘要保留 pre_compact_revealed_tools；assistant 思考、tool call、结果和媒体进入原始转录。

阈值唯一来源 `config.loader.resolve_model_config`：完整 C/O 时 R=min(O,20k)，自动线 C-R-13k、
阻断线 C-R-3k；缺项时自动线 C-64Ki。`estimate_tokens` 的媒体预估不是模型容量或预留公式。
provider exact usage 加增量用于本 run 的触发判断；压缩成功清除旧基线。

检查点 v2 只记录已提交摘要 ID，不保存另一份私有模型视图。旧 v1 检查点由离线迁移备份清理。

## 缓存前缀与压缩请求

2026-11-09 用户指定按 Kimi K3 缓存有效期 30 分钟分析；不将其推广为所有 Kimi 模型与端点的默认值。当前本机 `system.json` 的 `k3.cache_ttl_seconds` 仍为 3600，后续配置应与该前提一致。这个字段目前仅通过模型解析进入 run 快照，没有发给服务端，也没有执行缓存保活；修改它不能延长远端缓存。

2026-11-09 用户授权后已在源码完成以下修改，尚未部署；Bash、保活与生产配置没有变化。

### 当前压缩路径

- `DefaultHarness._model_request` 是普通请求与压缩共用的请求组装规则，负责会话 system、工具范围/已揭示状态、原生搜索、thinking 与缓存路由。`Harness.compact` 准备模型服务与摘要函数，生命周期交给唯一的 `ContextSDK.compact`。`/compact` 经原有 AgentRequestMapper 获取当前 agent 的配置；活动 run 直接复用其 HarnessContext，不创建新 agent、会话或 RunRecord。
- 首次摘要请求直接追加现有 `get_compact_prompt()`，保留完整历史的角色、工具调用/结果、思考内容和媒体；保留 model、effort、tools、metadata 与输出上限。不预先截到 256k、不分批转录、不另起摘要 system、不强制关思考。旧 `_summary_transcript`、`_summary_batches` 和分批合并提示已删除。
- 保留工具 schema 不表示允许执行工具。摘要流只收集文本；出现 tool_call 就拒绝该次输出，不调用执行器，不写工具结果。已有摘要提示词保持原文，未自行补写重试或恢复提示词。
- 总计至多 3 次尝试。网络/服务端可重试错误、无效格式、输出截断、工具调用及摘要没有缩短，均只重试当前同一请求；不因此裁剪历史。鉴权等不可重试且非容量错误直接失败。只有明确的上下文容量错误才启动有损恢复，不能把一般 bad_request 或媒体数量限制当成上下文超限。
- 超限恢复采用 Kimi `725194d7` 的 `shrinkCompactionHistoryAfterOverflow` / `takeRecentMessagesWithinTokenBudget`：按当前历史的 token 估算及源码比例 `[0.7, 0.5, 0.35]` 选取最近的完整消息，去掉开头孤立 tool 结果；保持 system、schema 和所有保留消息原文。不砍半条文本、不拆媒体列表、不复制旧 user 消息当引导、不补重试占位词。若单条消息太大、选不出可用后缀或后端拒绝其结构，则明确失败，原上下文不变。Kimi 的预裁剪未照搬；按用户要求，第一次始终直接使用全量上下文。
- 最终结果须有闭合的 `<summary>`，只允许已有格式的可选 `<analysis>` 包络，不能是工具调用或输出截断。仅用于进度的部分摘要可以未闭合，不能据此提交。提交前比较摘要正文加恢复模板/转录路径后的预估占用，必须严格小于原消息。该检查是输出协议及长度检查，不保证摘要语义无损。
- 摘要通过验证后先保存并同步完整原始转录，再替换冻结前缀；后续消息、队列、任务和会话身份保留。生成失败/取消/无效摘要不提交，磁盘与前缀校验仍沿用原持久提交保护。成功后只有摘要的会话不会继续反复压缩。

主路径参考 Claude 的缓存共享与 Kimi 共用 requester；超限恢复采用上面注明的 Kimi 当前源码策略，不照搬 Claude 另一条更换 system/关闭 thinking 的独立摘要分支，也不保留原先的分批转录实现。最初自行补入的文本/媒体砍半、输出异常时裁剪与旧 user 锚点均已撤掉。Kimi 官方说明切换 thinking effort 会导致缓存失效，见[模型配置](https://www.kimi.com/en/help/kimi-code/models)。

压缩请求可复用旧长前缀；压缩后的摘要是新的消息前缀，旧历史部分通常不能继续命中，稳定 system/tools 部分仍可能复用。实际缓存命中仍须看端点 usage。当前摘要 usage 尚未纳入主 run 累计统计；离线请求结构一致性测试不等于服务端命中率实测。

### 验证范围

`test_compact_functions.py` 验证 Kimi provider body 的原始前缀与配置一致、长文本/媒体首次不裁剪、容量失败后的完整消息选取、单条消息不切片、无效或更长摘要重试原请求、工具调用与截断拒绝。`tests/context/` 验证嵌套可变数据隔离与持久提交后报错时校准视图。`test_command_compact.py` 验证忙时维护边界、重启后配置复原、无新增 RunRecord，以及更长或异常摘要不改 SDK/主历史。原持久化、取消、恢复、工具揭示状态和普通前缀测试仍适用。

2026-11-09：全量离线回归 1295 项通过，旧生命周期入口已删除，未部署；服务端实际缓存命中率尚未实测。

### Claude 与 Kimi 请求链复核

后续按用户要求追到实际请求组装，而非仅看压缩函数的入参：

| 实现与版本 | system 与工具定义 | 历史与 thinking | 代码依据 |
| --- | --- | --- | --- |
| Claude Code 2.1.88，优先的缓存共享分支 | 从主会话 `CacheSafeParams` 继承 system、上下文与工具选项 | `forkContextMessages + summaryRequest`，保留 thinking；不覆盖可能改变 thinking budget 的输出上限 | `services/compact/compact.ts:1151`、`:1181`；`utils/forkedAgent.ts:49`、`:524`、`:545` |
| 同版 Claude，缓存共享关闭或失败后的降级分支 | 换成专门的摘要 system；工具为 FileRead，启用 tool search 时增加 ToolSearch 与对应 MCP 工具，并非空工具集 | 正常消息归一化，去除图片/部分重注入附件；关闭 thinking，可单独设摘要输出上限 | `services/compact/compact.ts:1250`、`:1281`、`:1292` |
| Kimi Code `725194d7` | 压缩入口不覆盖 system/tools；共用 requester 从 profile 获取 system，tool-select resolver 补入当前默认工具 | 在历史末尾追加摘要指令；经正常历史整形通道，思考设置来自 profile；窗口不足时会预缩减/溢出恢复 | `fullCompaction/fullCompactionService.ts:620`、`:647`；`loop/requester.ts:724`、`:760`；`loop/toolSelectPolicy.ts:5` |

上述 Kimi 路径均相对 `packages/agent-core-v2/src/agent/`。工具、profile 或窗口发生变化时不能据此保证完整前缀永远相同。Kimi 还在 requester 中单独记录 compaction 的缓存 usage 探针（`loop/requester.ts:533`），缓存效果须看实际 usage，而不只看结构。

Claude 主分支保留完整工具 schema，但用 `createCompactCanUseTool` 拒绝实际工具执行（`compact.ts:1125`），二者不是同一件事。源码中压缩期间的 WebSocket/worker 心跳用于连接存活，不能当作模型 KV 缓存续期的证据。

版本边界：npm 最新版本核实为 2.1.319；本机 2.1.318 二进制包含缓存共享成功、fallback 等相关字符串，只能作为字符串存在的证据，不能据此还原控制流。上表 Claude 的逐行结论仍限定于 2.1.88 可读源码，不声称已核查最新版完整实现。

旧实现中“CC: compact 调用关思考”的注释已随旧摘要构造路径删除。它把 Claude 的降级策略概括成通则；当前实现不再以它为依据。

### 用户原话与任务进度：Kimi / Codex 当前机制核查

按用户要求，本节只以当前 Kimi 与 Codex 源码为依据，不用旧 Claude 材料推断。2026-11-09 核对远端 HEAD：Codex 为 `8703d32bd0ea5cf26768859a3dd476df906ba053`；Kimi 本地已从 `725194d7` 快进到 `cd48f5e84b74ddcd91c40e25d59470d43a05b6a0`，该次变更只涉及 skill 调用，本节相关实现未变。以下核查覆盖代码与对应测试断言，未运行两家的完整测试工程，也未修改 Xana 运行代码。

**Kimi 的 TODO 是独立的持久状态，模型上下文和界面读取它的投影。**

- `features/todo/todoService.ts::AgentTodoService` 用 actor 持有清单，正式更新经 `ToolsUpdateStore` 事件，事件在 `todoOps.ts` 明确标为 durable；状态可从 wire 日志恢复，并参与 undo，不依赖某条工具消息是否仍在模型上下文中。`sessionTodo.test.ts` 覆盖主/子 agent 隔离、持久事件和重启恢复。
- `TodoList` 工具不传 todos 时读取服务，传入时全量替换服务中的清单。`kap-server/.../sessionAgentFeed.ts` 订阅同一个服务的 onDidChange，发送 `type: todo` 与 items；界面不是从模型摘要重新提取一份清单。
- `fullCompactionService.ts::postProcessSummary` 在模型摘要完成后读取 `this.todo.get()`，以确定性渲染把实时清单附到摘要后。摘要指令明确告诉模型不要抄 TODO，因为宿主会补上它。对应测试 `fullCompaction.test.ts` 的 `appends the todo list to the compaction summary` 直接断言此行为。因此模型漏写清单，不等于权威清单被删除。
- 完成状态仍由调用工具的模型维护；durable、UI 和自动回填保证存储/传播，不证明每个标为 done 的工作真的完成。背景进程/子 agent 的执行状态属于任务服务，不能与 TODO 的声明混为一谈。

**Kimi 同时显式保留真实用户消息。**

`contextMemory/compactionHandoff.ts` 依据 origin 区分真实用户输入与 task/cron/injection/summary 等自动消息。压缩后的上下文组合用户原话、摘要与续跑注记；用户消息默认预算 20k tokens，超出时为最早输入留 2k，其余留给最近输入，中间省略会标记。边界处可能截取用户文本，不能理解为所有历史原话始终完整保留。这是摘要成功后的上下文保留规则，与摘要请求超限后的恢复裁剪是两件事；不能直接把这些数值当作 Xana 的产品决定。

**Codex 的 checklist、用户输入保护与长期 goal 是不同机制。**

- `tools/handlers/plan.rs` 的 `update_plan` 发 `EventMsg::PlanUpdate`；app-server 转成 `turn/plan/updated`，TUI 计算完成数量并显示清单。`rollout/src/policy.rs` 明确将 PlanUpdate 事件列为 transient/non-durable。工具调用参数本身仍属于模型调用历史，但这不能等同于独立的持久 TODO 服务。
- 沿当前开源客户端的这条 checklist 链，没有查到 Kimi 那样从独立服务强制把最新清单回填进摘要的路径。不能由进度 UI 推断 checklist 已具备同样的压缩恢复保证，也不能据此声称远端压缩一定会丢进度；远端 checkpoint 内容并不全部由客户端实现公开。
- `compact.rs` 的本地摘要重建路径选取真实用户消息并保留原始内容/来源标注，预算为 20k tokens，优先最近输入；`compact_remote_v2.rs` 有不同的保留规则与 64k 总预算，并可保留特定注释、消息或工具输出。预算不足仍会截断并标记来源不完整，不能承诺所有原话无损。
- 长期目标则有真正独立的数据：`state/src/runtime/goals.rs` 的 thread_goals 持久表，以及 `ext/goal` 的状态、用量、生命周期和 steering。它不能拿来证明 update_plan 的 checklist 也使用相同的持久机制。

**Xana 当前缺口与待决定方向。**

已有 `TodoStore` 在会话 JSON 中独立持久化，`TodoWrite` 更新它；但 `ContextSDK.compact` 的结果当前仍只由模型摘要生成，未像 Kimi 一样确定性回填实时 TODO，用户原话也未独立保留。普通循环只在满足 10 步提醒条件时附上清单，因此“磁盘上没丢”不等于“压缩后模型立刻可见”。

建议将用户原话保留规则、任务状态与压缩投影明确为不同职责：真实用户输入保留来源及原文；现有任务状态只有一个权威来源，工具与 UI 共用；ContextSDK 在压缩提交时只生成其只读投影，不从摘要反写真实进度。这不需要再造第二个上下文生命周期管理器，也不意味着必须把任务执行状态塞进 ContextSDK 的消息序列。具体预算、保留范围及完成验证规则尚待产品决定，本轮不擅自移植上游常数或添加模型指令。

若摘要本身包含用户话语，仍须保留其摘要来源，不重新当作真实用户消息加入原话保留区。Kimi 使用 `origin.kind=compaction_summary` 排除这种递归收集；少量语义重复不能成为删除真实用户输入的理由。与 Web 工作区方向及后续迁移的衔接见[工作区设计](../docs/design/project-capability-design.md#压缩摘要包含用户原话时)。

### 长任务等待期间的缓存保活

缓存过期不会中止本地命令或删除 Xana 的持久上下文；影响的是之后模型请求的重算、延迟与计费。当前 Bash 最多 600 秒即终止，尚不存在一次 Bash 工具调用直接等待 30 分钟的正常路径；将来支持长命令后才出现这一场景，现有 agent/eval 等待也可能产生长间隔。

可研究独立于会话消息流的保活请求，但它仍须携带要续期的完整缓存前缀，或使用该端点明确提供的续期句柄；短心跳及仅重复 cache key 不足以刷新大上下文。当前 Kimi provider 未接入仅续期接口。开放平台文档说明命中可续期，但不能据此保证 Coding 端点相同行为，须验证命中是否续期及配额消耗，见[官方缓存公告](https://forum.moonshot.ai/t/context-caching-upgrade-5-min-1-hour-ttl-options/613)。

建议复用最近一次有效请求的快照，避免复制当前尚缺结果的 tool_call；保持缓存相关参数一致，不新增“保活”提示词、不启动另一轮 agent，不执行返回的任何工具调用，也不把保活返回写入对话或替换最后正式请求。输出上限取 provider 支持且不破坏缓存身份的最低值，并消费实际 usage；输出极小不等于请求免费。

触发条件应同时包含：真实任务仍在等待、相同前缀预计继续使用、临近 30 分钟有效期且留出网络余量、经济上划算。按会话及缓存身份去重；正常模型请求刷新计时。任务结束、取消、压缩、模型/工具/思考配置变化或服务关闭时撤销后续保活；已发出请求的竞态费用仍需记录。不要为纯空闲会话无限续期。

设共享前缀 C tokens，累计额外保活 n 次，缓存读取单价 Ph，未命中单价 Pm，最终恢复同一上下文的概率 p。仅在 `n*C*Ph + 保活输出/写入等费用 < p*C*(Pm-Ph) + 可避免的冷写入费用` 时，保活才可能节省费用。应设置次数/费用上限，观测到未命中或异常后停止，全部费用计入原任务预算；不得把保活 usage 当作当前对话长度。即使单次成本随 C 增长，长期反复读取也可能比允许一次过期更贵。

测试：`test_compact_persistence.py`、`test_compact_resume_integration.py`、`test_command_compact.py`、
`test_harness_auto_compact.py`、`test_multimodal.py`。
