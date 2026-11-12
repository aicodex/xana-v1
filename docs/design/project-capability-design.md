---
title: project 主 agent 与可复用能力设计
status: proposed
date: 2026-11-08
updated: 2026-11-09
---

# project 主 agent 与可复用能力设计

Xana 面向长期任务和持续迭代。建议一个 project 默认由一个主 agent 负责目标、用户反馈和交付，反复验证的方法沉淀为 skills 和 tools；需要独立执行上下文时再委派子 agent。这里的“一个”描述默认职责归属，允许多个会话、并发 run、定时任务和必要的专用 agent。

本文保存参考经验与 Xana 设计建议。具体迁移须经过功能评估；现有 project、会话、委派和后台运行契约继续有效。

## 2026-11-10：project、Agent 与 skill 的用户澄清

- project 是独立工作区、记忆和迭代的边界。产品入口围绕创建项目，弱化“为每个功能创建 agent”的旧概念。
- Agent 是有持续身份与状态、可长期运行的执行主体；一次 run 是其一次执行，run 结束不等于 Agent 身份消失。subagent 强调独立执行主体；项目主执行入口仍属于项目。
- skill 是 Agent 按需加载的能力和工作方法，不拥有独立运行生命周期，不把每个 skill 重新做成特殊 agent。
- 后续可能有迭代 Agent 等特殊角色；这是设计空间，不在本次 memory 改造中批量迁移、删除已有 Agent 定义或重写迭代实现。
- 用户提出“未来 checklist 也许由专门 Agent 维护”仅作为备选方向，**先不实施**。当前持久清单、压缩后恢复和 Web 投影保持现有机制。
- memory 使用每项目独立的 `memory/` Markdown 原件与派生 SQLite 索引；SAG2 接入见[记忆检索方案](memory-sag2-design.md)。

## 2026-11-09：Web 工作台与可发布工作区方向

用户明确的新方向：Xana 是面向长期运行和持续迭代的 harness；Web 为主要工作界面，飞书与 Telegram 为辅助聊天及通知渠道；不再持续建设完整 TUI，终端仅保留安装、配置、启停、诊断和简单交互。一个 project 是独立工作区，拥有自己的主 agent、skills、tools、记忆/知识和运行状态；聊天接入是工作区的外部入口，运行核可作为 SDK/headless 服务独立使用。

方向已记录，下面的接口、目录和发布拆分仍是迁移设计，不表示已经完成。不得由此直接删除正在使用的客户端、变更服务进程布局或重启定时任务。Web 的核心路径可用且完成迁移后，才删除替代掉的旧展示逻辑，不长期维护两套完整工作台。

### 职责边界

| 部分 | 责任 | 发布方向 |
| --- | --- | --- |
| 运行核 | 模型/工具执行、单会话循环、上下文生命周期、任务与调度、预算及恢复机制 | 随业务能力包提供必要运行依赖，可独立使用 |
| 工作区 | 主 agent 定义、业务 skills/tools、选定记忆和知识、依赖声明与采用版本 | 是能力发布的来源边界 |
| 平台创建/迭代能力 | 创建项目及主 agent、修改业务组件、评测、比较和采用版本 | 以特殊 skills 编排；默认不随业务包发布，最终范围见下方待确认项 |
| Web | 工作区与会话入口、执行过程、调试、进度、日志、产物、用量与调度可视化 | 主要维护一套工作台；不成为运行依赖 |
| 飞书 / TG | 简短交互、关键结果、需要用户处理的通知及控制、跳转 Web 查看过程 | 接收方按需要配置，不是运行核的必需依赖 |
| 简单 CLI | 安装、初始化、服务管理、诊断及有限交互 | 保留管理能力，不继续扩充成另一套复杂界面 |

skills 承载业务方法和迭代方法；预算执行、权限、任务状态、版本采用与恢复仍有正式运行契约，不能仅写进 SKILL.md 后要求模型自觉遵守。创建 agent 的流程应围绕工作区的主 agent 与能力组合调整；必要的临时委派仍可存在，不把每个业务步骤重新做成长期独立 agent。

特殊 skills 的安装、向哪个 agent 注册、何时加载继续分开。平台维护入口才能使用的创建/迭代/发布工具，仍由执行边界限定，不因发布为一个 skill 就自动向所有 agent 开放。

### Web 参考与首版范围

参照 [Kimi 官方界面说明](https://www.kimi.com/en/help/kimi-code/desktop-interface-and-sessions)的信息组织：工作区/会话导航、主对话与执行时间线、任务栏、侧边详情、命令输出。Kimi 当前仓库的浏览器前端原始源码不在公开仓库内，但包含同步的构建产物；本轮核查了构建产物和 `packages/kap-server` 服务端，核查范围与证据见下节。

首版围绕一个工作区主视图完成以下闭环，不拆成多个相互独立的界面系统：

1. 工作区选择、会话列表、主对话、插话、停止与已有恢复入口；权限提供“始终询问、必要时询问、全自动”三档。
2. 维护 checklist 与后台任务列表；子 agent **运行期间**即可打开其会话，查看实际派发 prompt、已产生的回答、工具调用和实时变化；任务结束后仍可查看。
3. 命令日志面板：实时跟随、按任务/工具调用定位、刷新后继续读取。模型收到的有限结果预览与用户可查的完整日志分开，不能用“模型结果已截断”当作完整执行日志。
4. 同一主视图内展示长期目标、用量/预算、定时任务状态与下次执行时间。浏览器断开不停止任务，重连从持久状态和事件游标恢复。
5. 业务产物及 agent/skills/tools 的变更查看；创建与迭代入口在同一工作区内展开。完整交互终端、浏览器和 IDE 能力不作为首版前提。

UI 只读取内核权威状态的投影。会话历史、任务、进度、日志及预算必须能独立查询；实时事件用于刷新，不能成为唯一数据来源。飞书/TG/CLI 不各自实现计划、任务或取消状态机，也不各自创建一套模型上下文。

用户明确排除 swarm 和 tower，不因参考 Kimi 而顺带引入。这不排除普通子 agent 委派、后台执行或长期 goal。Checklist 也不等同于要求先审批方案的 Plan 模式。

后续用户已确认新建会话默认“必要时询问”。具体开工顺序、持久数据迁移及验收见[Web 工作台实施计划](web-workbench-implementation-plan.md)。

现有 `/submit` + SSE 可以作为已有输入/事件能力的起点，不因 Kimi 使用 WebSocket 就另造协议。需要补齐浏览器可用的同源认证、历史分页、任务详情、日志读取、状态快照及重连回放边界。模型执行继续使用当前唯一 harness 链。

### Kimi Code Web 源码核查：2026-11-09

本轮将本地 `best_agent/kimi-code` fast-forward 到 `27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9`。以下来源链接固定到该版本；结论来自服务端、运行核、相关测试和随包发布的前端 JavaScript，不声称运行验收了 Kimi Web。前端原始工程在私有 `code-app/apps/web`；公开产物 `apps/kimi-code/dist-web/assets/index-D6B2p7sa.js` 无 source map，可确认协议调用、组件和实际文案，不能据此补造其原始组件目录。

#### 三档权限的真实含义

| Xana 展示名 | Kimi 内部值 | 随包 Web 的实际含义 |
| --- | --- | --- |
| 始终询问 | `manual` | 默认自动读取，其余操作询问；不是每次 Read、搜索、查询进度都弹窗 |
| 必要时询问 | `yolo` | 常规修改、命令自动执行；高危操作、向用户提问和计划审阅仍可能等待用户 |
| 全自动 | `auto` | 操作和判断自动完成；显式 Plan 模式的计划仍另有审阅规则 |

文案直接见[前端构建产物][kimi-web-bundle]中的 `permissionManualDesc`、`permissionYoloDesc`、`permissionAutoDesc`。这是 Kimi 的语义参照；“始终询问”是否要比 Kimi 更严格，仍以用户后续明确要求为准，本轮未改 Xana 默认权限。

内核细节不能由三个文案推导：

- [PermissionPolicyService][kimi-web-policy]按顺序选取首个命中的 allow/deny/ask 策略；显式禁止先于自动放行。`auto` 下高危命令检查主动跳过，`AskUserQuestion` 则直接禁止，让模型自行判断。不能误说 Kimi 的全自动仍逐项询问高危命令，也不能说它绕过一切禁止。
- `manual` 的默认放行名单还包含 TodoList、任务查询/等待、Skill、Agent 等，另有 Git 工作区写入例外。因此上表是产品语义，不是完整权限矩阵；Xana 的具体工具分类须在实现前逐项列出，不能仅按工具名字猜测。
- `PermissionGateService`在真实工具执行前消费策略，ask 等待审批；Web 通过 interaction ID 回复批准、拒绝、取消或本会话批准。不是先执行再显示审批。
- 模式由 agent 状态持久化；会话配置更新可广播给当前活动 agent，普通子 agent 创建/恢复会继承父权限，固定权限 profile 另有规则。Xana 的普通委派也不能提高父级权限；工具可见范围、沙箱与审批模式分别约束执行。
- [interactions 路由][kimi-web-interactions]校验会话和未结束的交互；Web 订阅会话时会补发当前 pending interactions。重连后仍能处理子 agent 的待审批操作，不必一直停留在子会话面板。该补发不等于进程重启后会自动恢复原执行。

#### Web 的数据与执行链

1. `kimi web` 在 CLI 当前进程启动 `kap-server`，加载 `dist-web`，打开浏览器；[webAssets.ts][kimi-web-assets]负责静态资源与 SPA 回退，API 路径不落入 SPA。Xana 长期运行沿用服务端进程所有权，不照搬关闭 CLI 即退出服务的生命周期。
2. Web 用 REST 提交输入、查询历史和回复交互，用 WebSocket 订阅变化。初次订阅可以指定多个 `agent_ids`，默认主 agent；服务端返回最近一页 turns，再发送 turn/item 增量、状态与任务变化。新增查看者能看到运行中已流出的内容，不必等下一次模型请求才开始显示。
3. [ConversationService][kimi-web-conversations]对主/子 agent 使用同一个查询入口。历史来自持久 wire 记录的投影，运行中的 delta 再覆盖到当前视图；展示历史与供模型使用的压缩上下文不是同一份可写状态。REST 按 `before_ordinal` 分页，默认 50 turn、单页最多 1 MiB；这些是来源参数，不直接当 Xana 默认值。
4. [任务协议][kimi-web-tasks]的 `child_agent_id`、`parent_tool_call_id`把后台任务、发起工具调用和子会话关联起来。前端产物可定位到 `AgentDetailPanel`、`openAgentPanel` 和 `auxiliaryConversations`：点击 agent 打开详情，按子 agent ID 查询/订阅 turns，并支持加载更早消息和显示 running 状态。
5. 命令输出另走 `task.watch`，使用 `since_offset`续读；它与子 agent 的完整对话订阅不同。关闭面板或 socket 只释放观察资源，`task.stop`才要求停止任务。不存在“先 attach 才能看”的要求。

Kimi [会话协议端到端测试][kimi-web-e2e]分别覆盖后台子 agent 关联、订阅正在运行的 turn、REST 与 WS 历史一致、TodoList 更新推送、输出 offset 重连，以及子会话服务重启后续接。本轮阅读了测试及关键断言，未执行；上游 checkout 未安装 node_modules，未为调研安装依赖或启动其服务。

#### Goal、checklist 与打断

| 对象 / 操作 | Kimi 实现与需要保留的边界 |
| --- | --- |
| Goal | 主 agent 独立持久状态：目标、完成条件、状态、tokens/turns/耗时及可选上限；不是一段 system prompt 或 UI 标签 |
| Goal 继续执行 | 一轮结束后检查目标状态和预算，经同一个 loop 提交 continuation；主回答、用户输入和 goal 续跑共享准入，不启动第二个前台执行器 |
| Goal 暂停 / 取消 | 暂停可恢复，取消清除当前目标；用户打断或执行失败会停住自动续跑。恢复后的 active goal 默认规范化为 paused，不能把“保存目标”等同于“重启就自动再跑” |
| Checklist | 每个 agent 的 TodoService，`get/replace/clear`及 durable `tools.update_store`事件；Web 从同一状态读取，主/子清单相互独立 |
| 压缩 | `FullCompactionService.postProcessSummary`从实时 TodoService 读清单，确定性追加 TODO List；不要求模型从摘要猜当前进度 |
| 普通发送 | 活动 turn 中提交的新 prompt 可排队，之后形成自己的 turn；不会因为发送新消息而清空已完成工作 |
| 插话 steer | 把已排队输入提升到当前 turn，保留用户来源和 FIFO 顺序，进入下一次可消费输入的边界；不是丢弃整个 turn 从头再执行 |
| 停止当前回答 | 独立 cancel/abort 控制，不排在模型回答后等待；补齐执行收尾与已产生历史。已交出的后台任务与直接持有的执行分别处理 |
| 停止后台任务 | 明确指定 task ID，与停止等待、停止父回答不同；查看子会话不改变任务所有权 |

源码依据：[GoalService][kimi-web-goal]、[TodoService][kimi-web-todo]、[压缩后处理][kimi-web-compact]、[prompt 路由][kimi-web-prompts]、[agent 状态机][kimi-web-machine]。steer 的状态机测试明确在慢工具尚未返回时加入插话，随后保留该工具结果并继续；abort 测试则分别检查正在执行的工具和已交出后台任务。不能把“插话及时接收”宣传成“任何命令都能立即无损撤销”。

#### Xana 的落点与迁移顺序

这些是待实施设计，不是已上线能力：

| Xana 当前基础 | 所需工作与所有权 |
| --- | --- |
| `policy/contracts.py`有七种 mode；各工具 `check_permission`，扩展可复核 non-allow | 在唯一权限执行边界落实三档策略和待审批交互。先清点枚举调用方/持久配置；Plan 与沙箱约束独立表达，不能直接删除对应限制。不要每个渠道实现一次策略 |
| `harness_sdk/goal.py`为独立模型评估的 stop-hook；`DefaultHarness`每轮调用 | 评估原位迁移为持久 goal 生命周期和统一续跑。先列出现有完成判断、配置及调用差异，再实施；不得叠加另一套 Kimi goal 循环，造成双重续跑或额外评估费用 |
| `TodoStore`已有会话 JSON 和 `todo_write` | 复用一份清单状态，补正式读取/变更通知和压缩后投影；不要 Web 再存一份，不在第二个模块写上下文。维护入口显式注入，不继续经 metadata 隐藏传依赖 |
| `BackgroundTasks`持久记录与子会话 ID 已存在 | 补任务详情和父子关系查询，子会话复用普通历史与实时事件入口；模型仍决定委派、等待或先回答。首版子会话面板以查看为主，独立编辑/对话控制不属于已确认范围 |
| `TurnCoordinator`已有单循环、输入队列与取消 | Web 复用当前插话/停止契约；普通发送是否默认插话以现有 Xana 行为为准，不因 Kimi 有排队入口而悄悄改变。新增 goal 自动续跑必须尊重用户停止 |
| `daemon_http` + bridge ring/SSE | 补浏览器同源鉴权、工作区/会话发现、分页历史、快照和重连恢复；明确现有 max5000 内存 ring 的缺口，不能把进程内缓存当持久历史 |
| `ProcessRunner`在命令返回时提供合并输出 | 补执行时落盘/读取的日志及游标；完整日志与模型预览分开。命令后台化是否进入实现范围须另列影响，不能只加一个展示面板就声称支持 |

第一阶段先打通一个工作区、主会话、实时流、停止/插话和三档权限审批；审批交互必须可在模型执行期间独立送达。第二阶段接入同一主视图内的持久 goal、checklist、后台任务列表和**运行中子会话**，以及必要的命令日志读取。两阶段合起来才算用户要求的首版闭环，不能把第二阶段当作长期可选项。swarm、tower、复杂 TUI 均不进入这次实现。

验收至少覆盖：

- 同一读/写/命令在三档下的判定；拒绝、取消、本会话批准；子 agent 权限继承、待审批来源、切换会话后仍能响应；重复点击不重复执行。
- 主 agent 回答中发送插话、慢工具期间停止、停止与任务完成竞态；保留已完成结果，不自动复活用户停止的 goal。
- 子 agent 仍在运行时打开详情，刷新、关闭再打开、加载历史；prompt/工具调用/结果逐步显示，父子事件不串线；主会话仍可继续交互。
- 主/子 checklist 隔离、服务重启、上下文压缩；goal 暂停/恢复/阻塞/完成、预算停止及重启恢复；Web 只是投影，不另存权威状态。
- 浏览器断开后任务继续，重连快照补齐；命令日志按 offset 续读，缺口有明确表示；当前后台结果固定身份与去重测试继续通过。

本轮没有修改模型可见提示词、运行代码、默认权限、持久数据或服务进程。后续工具名、说明、goal 续跑与上下文注记继续遵守[提示词来源约束](../../doc_ai/harness_prompt_assembly.md#提示词来源约束)：先核查上游当前来源，再翻译中文；不能把本文面向开发者的设计建议直接当成额外模型指令。

[kimi-web-bundle]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/apps/kimi-code/dist-web/assets/index-D6B2p7sa.js
[kimi-web-policy]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/agent-core-v2/src/agent/permissionPolicy/permissionPolicyService.ts
[kimi-web-interactions]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/src/routes/interactions.ts
[kimi-web-assets]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/src/routes/webAssets.ts
[kimi-web-conversations]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/src/services/conversation/conversationService.ts
[kimi-web-tasks]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/src/protocol/ws-task.ts
[kimi-web-e2e]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/test/conversationContract.e2e.test.ts
[kimi-web-goal]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/agent-core-v2/src/features/goal/goalService.ts
[kimi-web-todo]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/agent-core-v2/src/features/todo/todoService.ts
[kimi-web-compact]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts
[kimi-web-prompts]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/kap-server/src/routes/prompts.ts
[kimi-web-machine]: https://github.com/MoonshotAI/kimi-code/blob/27bd9baba10e9f73d3db0e0fdd6f81046b4cbac9/packages/agent-core-v2/src/human/agent/machine.ts

### 发布：通用入口与 Xana 运行核

采用通用 [Agent Skills 格式](https://agentskills.io/specification)：SKILL.md、scripts、references、assets。`npx skills` 是技能安装入口，不能将“宿主发现了技能”解释成“已安装 Python 依赖并具备 Xana 的任务、预算、记忆和恢复机制”。官方项目见 [vercel-labs/skills](https://github.com/vercel-labs/skills)。

一份能力包声明业务入口、所需 skills/tools、运行核版本、业务依赖与配置要求。装到 Codex/Claude Code 时，标准 skill 作为入口调用随包提供的 Xana 运行能力；要复现 Xana 特有行为，由同一运行核执行，不让不同宿主重新解释一遍业务工作流。完整安装入口负责依赖检查、初始化和配置；技能安装入口与完整安装入口使用同一份清单，不维护两份业务实现。

发布边界待本轮用户确认：初步理解“核心工作流随包”是业务执行需要的内核与工作流随包；用于创建、调试、迭代工作区的特殊 skills 留在 Xana 平台。若用户要求接收方也能使用同一套迭代能力，需要显式调整这个边界。

记忆与知识的发布以明确选择的数据及依赖为准：业务记忆、知识源、检索适配和初始化要求可以随包提供；可重建索引声明重建方式，外部知识库由接收方配置连接。活动上下文、正在运行的进程、平台维护数据与业务记忆不是同一种发布对象。

### 当前代码与方向的落差

- `runtime/daemon_http.py` 目前主要是 CLI bridge：有限内存 ring 回放、submit/stream/cancel，且拒绝所有带 Origin 的请求；不能直接当成具备持久会话查询和任务详情的 Web API。
- `tools/builtin/_process.py::ProcessRunner` 当前在返回时合并 stdout/stderr，没有完整的、可分页且可重连跟随的命令日志服务。Web 展示应补执行侧日志契约，不能从聊天卡片反推原始日志。
- `publishing/artifacts.py::build_runtime` 当前遍历整个 Xana Python 包，不能保证创建/迭代组件物理上不在发布包中。需先分清运行依赖与平台维护组件，再收紧构建产物；仅在 skill 列表中隐藏不等于不打包。
- `publishing/package.py` 已有能力包、运行核 wheel 和 SKILL.md 入口，但当前生成文案、安装流程和完整运行依赖尚不能据此认定已满足通用宿主的一键使用验收。
- 当前用户原话、实时 TODO 回填与压缩结果的边界仍需按下面的压缩规则设计；本轮不擅自把上游的 20k/2k 数值移植为 Xana 默认配置。

### 压缩摘要包含用户原话时

Kimi 的 `compactionHandoff.ts` 用 origin 区分真实用户输入、摘要和自动注记。摘要即使被放在 user 角色中，仍标为 `compaction_summary`，不会在下一次原话保留时再次被当成真实用户输入收集。参考版本为 `cd48f5e84b74ddcd91c40e25d59470d43a05b6a0`。

用户原话是事实来源，摘要是模型对任务、进度和决策的转述。摘要可以提炼用户意图，也须保留没有进入原话保留区的关键约束；已经单独保留的原文无需整段重复抄录。少量语义重复可以存在，不能为了省 token 用文本相似度删除真实用户消息，或把摘要中的“用户同意”提升成新的授权。

实时 TODO 从其唯一状态来源生成投影，压缩后由 harness 补回，不让模型在摘要里再维护第二份清单。连续压缩时，来源身份随持久记录保留；摘要里引用的用户话仍是摘要内容，不递归膨胀成一组新用户消息。具体实现仍由 ContextSDK 统一提交，不增加第二个上下文生命周期类。

### 实施顺序与验收

先完成可用的 Web 主路径及其必需的数据契约：项目/会话快照、进度/后台任务详情、持久命令日志和增量回放；使用当前模型执行链。随后接入文件变更、迭代入口和发布预览，再完善运行核与平台组件的构建拆分。已有聊天与调度持续运行，待 Web 验收后逐项减少旧 TUI 展示能力，不维护永久兼容别名或双重状态。

验收围绕真实长期任务：Web 关闭与重连、服务重启、压缩后继续、用户改口、后台完成/失败、命令中断、预算暂停及恢复、定时任务交付；检查原话来源、进度和已有成果没有丢失。发布验收另查同一选定业务版本在独立运行核与宿主 skill 入口上的行为，确认平台创建/迭代组件按最终清单排除。

## 原文经验

来源：Anthropic Applied AI 团队 Diane Sarkis，2026-10-22，
[Building agents: When to fold subagents into your main agent loop](https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop)。

原文建议将部分领域子 agent 折入主循环，用 skills 和工具按需提供能力，减少信息交接；上下文退化、独立并行、验证及权限隔离仍可支持委派。迁移前建立端到端评估，逐个比较并重复运行。这是部署经验，对 Xana 使用的 Kimi 模型仍需实测。

## Xana 的职责划分建议

以下为结合 Xana 目标提出的设计，不是原文对 Xana 的结论。

| 对象 | 主要职责 | 视频项目示例 |
|---|---|---|
| project | 目标、成功标准、能力依赖、持久状态和迭代版本的边界 | 用户反馈、素材、成片、评测与采用版本 |
| 主 agent | 理解反馈、选择能力、推进任务、对交付负责 | 决定重做哪些镜头及其对故事的影响 |
| skill | 可复用的方法、判断准则、步骤与参考资料 | 分镜、合并审核、连续性检查方法 |
| tool | 有契约的执行、实时查询、参数校验和失败处理 | 提交生成、查询任务、下载视频 |
| 子 agent | 在独立上下文中完成边界明确的任务 | 并行审核几组镜头并返回证据 |
| 持久状态与记忆 | 保存任务进度、产物、用户决定和经验 | 已完成镜头、生成任务 ID、返工原因 |

skill 可以携带脚本和参考文件，脚本执行仍经过工具。认证、权限检查和必须满足的约束由执行边界保证；skill 说明操作方法。稳定操作的调用方式应由组件承接，避免长期只存在于聊天记忆中。

主 agent 和子 agent 应加载同一份适用 skill。因权限、模型或独立验证而保留的专用 agent，可使用不同执行配置，但应尽量引用共同能力，避免复制整份领域工作流。技能的安装、对指定 agent 启用、单次执行加载是三个不同动作；委派不能扩大技能或工具的授权范围。

## 何时创建执行上下文

对 Xana，先判断任务需要共享哪些信息，再决定是否委派。创作、分镜、生成和审核持续依赖同一组用户反馈时，主 agent 加载相关技能通常更便于保持一致。独立研究分支、大量素材检查、需要避免生成过程影响的复核，则适合单独执行，返回结论、证据和产物位置。

子 agent 能把大量中间过程留在自己的上下文，但会增加模型调用和交接成本；是否值得应看总成本、耗时和任务成功率。创建一个业务功能时，先检查现有 skill 与 tool 是否足够，再决定是否增加专用 agent。

同一主 agent 可以运行多个会话和后台任务，各 run 保持自己的执行上下文。长期事实与任务状态应持久保存，不能要求全部历史始终驻留在一个窗口。前台异步占位符与 attach/detach 已删除；独立后台任务、单会话执行循环与调度继续遵守当前契约，定时任务无需持续占用前台对话窗口。

## 让迭代成果进入后续执行

视频 demo 暴露的问题是正式工具接口与活跃脚本脱节。后续设计应让项目明确采用的能力贯穿以下过程：

1. 探索时允许脚本试验，并记录成功调用与失败条件。
2. 操作稳定后，整理为工具；业务方法整理为技能；已有组件优先原位更新。
3. 用代表任务验证实现、参数契约和技能说明的一致性，再采用该版本。
4. 后续会话从项目能力目录发现并使用它；重启和压缩保留依赖、配置引用与任务状态。
5. 修改后重新验证并更新采用版本，完成调用方迁移后删除重复入口。

例如生成工具负责登录、会话续期和服务错误，模型负责传入生成意图。服务不可达时返回依赖失败；已经完成的镜头和生成任务仍有效。密码保存在运行配置或凭据存储中，能力包只记录配置需求。

用户反馈还应归入合适位置：通用审核方法更新 skill，稳定执行修复更新 tool，本次返工决定写入项目状态。这样同一条反馈无需在后续会话反复解释。

发布时，候选交付单元为项目采用的主 agent 配置、skills、tools、必要记忆和运行依赖。凭据由接收者重新配置；发布依赖应按实际使用整理，避免携带整个本机环境。

## 与现有结构的衔接

以下为工作区源码锚点，不代表其中所有功能都已部署：

- [project-design](project-design.md)：现有项目作用域和多会话设计；本建议补充主 agent 的默认责任归属。
- [AgentSpec](../../src/xana/agents/contracts.py)：已有工具选择、技能选择和目标契约。
- [SkillsListingContributor](../../src/xana/extension_api/builtin/skills_listing.py) 与 [SkillLoadTool](../../src/xana/tools/builtin/skill_load.py)：已有技能目录与按需加载入口。
- [agent_run](../../src/xana/tools/builtin/agent_run.py)：已有独立子会话委派与续接入口。
- [iteration-agent-design](iteration-agent-design.md)：已有版本和评测设计；需评估如何让日常聊天产生的改进进入同一套采用流程。

优先评估能力沉淀、发现和采用之间缺少的环节，再决定需要怎样调整 agent 创建引导、技能绑定及目录结构。涉及既有配置、行为或持久数据的迁移，先列出具体影响并确认。

## 迁移验证建议

先选一段真实工作流作比较，保持模型、输入和权限一致。评估至少覆盖正常交付、用户中途修改、工具失败、独立复核，以及“成功执行后重启并压缩再继续”。检查最终任务是否完成、技能与工具选择是否正确、已有能力是否退化，并记录总耗时和总调用成本。

每次只调整一个边界，多次运行并查看具体退化案例；保留有实际收益的委派。模型升级后重新评估。最终采用的版本同步更新契约、调用方、文档和验证，删除被替代入口。

## 对话执行机制的后续评估

[Codex 插话机制源码审阅](../../exp_ai/context_codex_steering.md)记录了单一活动 turn、输入队列与独立工具任务的控制边界。相关迁移已完成，当前契约见[会话、插话与取消](session-and-card-state.md)；Web 与聊天适配都复用这一条执行链，不恢复多 run 占位回填。
