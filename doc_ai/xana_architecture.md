---
title: Xana 架构设计与关键实现
type: doc
status: active
---

# Xana 架构设计与关键实现

## 1. 一句话定位

**Xana 是一个以 project 为隔离边界、以单会话单执行循环为核心、通过持久化状态和可重建视图支撑长程任务的 Agent Harness。**

它不训练基础模型，主要负责模型之外的执行、工具、上下文、记忆、权限、恢复、评测和迭代机制。

本文以当前源码为准；部分旧文档存在时间差，文中已按当前实现修正。

---

## 2. 总体架构

```text
CLI / Telegram / Feishu / Web / 定时任务
                    │
                    ▼
             Agent Adapter
       协议适配、会话路由、事件渲染
                    │
                    ▼
          DefaultAgentGateway
       入站接收、命令、取消、恢复、投递
                    │
                    ▼
             TurnCoordinator
       单会话单执行、queue / steer、取消
                    │
                    ▼
                ChatAgent
     AgentSpec + SessionConfig → HarnessContext
                    │
                    ▼
              DefaultHarness
      ┌─────────────┴─────────────┐
      │                           │
      ▼                           ▼
 Model Registry              Tool Registry
 Kimi / DeepSeek / OpenAI / Fake  内置 / 文件 / 扩展 / MCP
      │                           │
      └─────────────┬─────────────┘
                    ▼
        ContextSDK + HistoryMirror
      当前上下文 + 持久历史 + 原子压缩
                    │
                    ▼
      RunStore / SessionStore / RunEvents
                    │
                    ▼
          Todo / Goal / BackgroundTasks
```

横向能力：

- **Extension API**：提示词、工具、权限和生命周期扩展。
- **Memory**：项目长期记忆、知识树、召回与后台提取。
- **Evolution**：评测套件、版本指纹、episode、回滚和账本。
- **MCP**：外部工具发现、审核、发布、热加载与重连。

`src/xana/runtime/boot.py` 是唯一组合点。

---

## 3. Project 隔离与启动组装

### 3.1 项目边界

一个 project 包含：

- 独立 workspace；
- 独立记忆和知识；
- 独立 agents、skills、工具默认配置；
- 独立会话、run、日志和定时任务；
- 一个独立 serve 进程；
- 独立 daemon socket、token 和渠道绑定。

主要目录：

```text
~/.xana/
├── config/system.json          # 用户级模型、搜索等配置
├── credentials/               # OAuth 等凭据
├── agents/                    # 用户级 AGENT.md
└── projects/<id>/
    ├── project.json           # workspace 根路径
    ├── config.json            # 项目级覆盖配置
    ├── sessions/              # 历史、run、媒体等
    ├── scheduled_tasks.json
    ├── evolution/
    ├── run/                   # pid、锁、socket
    └── workspace/             # 项目工作目录
```

路径统一由 `src/xana/config/paths.py` 解析。

### 3.2 配置分层

```text
用户 system.json
    ↓
项目 config.json
    ↓
AgentSpec
    ↓
SessionConfig
```

- 用户层：模型、凭据、搜索等。
- 项目层：channels、daemon、sessions、tools、agent_defaults、mcp。
- Agent 层：提示词、工具、技能、模型偏好。
- Session 层：当前会话模型、effort、thinking、权限等。

普通配置遵循 boot read-once；MCP 有独立的局部热加载机制，不会重读并重刷整个系统配置。

### 3.3 boot 的职责

`boot()` 做依赖组装，不承载业务规则：

1. 加载配置并构造模型客户端。
2. 创建 ContextSDK、HistoryMirror、SessionStore、RunStore。
3. 创建工具、技能、agent registry。
4. 创建 Goal、Todo、BackgroundTasks、PermissionService。
5. 创建共享 DefaultHarness。
6. 创建 ChatAgent 和 Gateway。
7. 回放历史、扫描未完成 run。
8. 构造 MCP、评测和 episode 组件。
9. 返回完整 `BootResult`。

核心原则：组件可以替换实现，但不能在业务模块里偷偷建立另一套组合入口。

---

## 4. 请求全链路

一次普通请求：

```text
InboundMessage
  → Gateway 路由 session / agent
  → TurnCoordinator 准入
  → ChatAgent 组装请求
  → DefaultHarness
  → 读取 ContextSDK 快照
  → 拼接 system + notes + tools
  → ModelClient.stream
  → 解析 text / thinking / tool_call / usage
  → 工具执行
  → 工具结果写历史
  → 下一轮模型调用或结束
```

### 4.1 单会话单执行循环

`TurnCoordinator` 保证一个 session 同时只有一个活动 run。

收到新消息时：

- **queue**：持久化排队，当前 run 结束后按顺序启动；
- **steer**：用户显式提升后，在当前模型响应或工具批次的安全边界插入；
- 不启动第二个并行回答；
- 不取消正在执行的正常工具来服务插话；
- 不重放已有工具结果。

输入先持久化，再进入执行，重启后可以根据 input_id 恢复未消费输入。

### 4.2 观察与执行分离

- 执行 task 属于服务；
- SSE/Web/CLI 只是观察者；
- 关闭页面或断开订阅不会停止任务；
- 重新连接通过持久历史和事件游标恢复。

这是 Web 工作台、CLI 和聊天渠道能够共用执行链的关键。

### 4.3 Run 生命周期

`RunRecord` 保存：

- session/agent/run ID；
- running/completed/error/cancelled 等状态；
- parent run 和 lineage；
- budget；
- usage；
- started/finished 时间；
- exit reason。

进程异常退出后，`startup_scan` 将没有正常终止 marker 的 run 标为可恢复中断状态。

恢复时：

- 不重发原用户消息；
- 不自动重放副作用工具；
- 缺少 tool result 的调用写入“执行结果未知”；
- 使用当前主历史启动新 run。

---

## 5. Prompt 与上下文设计

### 5.1 静态 system 与动态 note 分离

静态 system 包括：

- 身份和沟通方式；
- 工具使用原则；
- 安全与风险操作；
- 输出格式；
- 环境段；
- AgentSpec 自定义模板；
- PromptProfile 尾段。

同一个 `(session, agent)` 内缓存并保持不变。

动态内容不改 system，而是作为 `ContextNote` 追加：

- 日期；
- 项目 AGENTS.md；
- 记忆索引、置顶和召回；
- 技能加载结果；
- deferred tools 名单；
- 模型切换；
- TODO 提醒。

关键不变量：

1. **只追加，不改写旧 note。**
2. **内容没变就不重复注入。**
3. **相邻请求尽量保持逐字节前缀一致。**
4. **工具 schema 与 system 尽可能稳定，提升 prompt cache 复用率。**

相关实现：

- `harness_sdk/system_prompt.py`
- `harness_sdk/context_notes.py`
- `tests/test_prefix_stability.py`

### 5.2 Agent 提示词模板

`AGENT.md` 正文支持：

- `${base_prompt}`：在默认提示词上扩展；
- `${env}`：插入环境段；
- `${extensions}`：插入扩展段；
- 不含占位符：完整替换默认提示词。

编码相关规则不放在通用 system 中，而是通过 `coding` skill 按需加载。

---

## 6. 上下文压缩

压缩不是简单调用一次摘要模型，而是一个原子状态转换。

### 6.1 职责划分

- `DefaultHarness.compact`：准备模型请求和摘要函数；
- `harness_sdk/compact.py`：摘要生成、协议校验与重试；
- `ContextSDK.compact`：冻结前缀、持久化、替换与恢复；
- `/compact`：通过 TurnCoordinator 排到维护边界。

自动和手动压缩共用同一实现。

### 6.2 提交流程

```text
冻结 SDK 前缀及历史行 ID
        ↓
保留原 system/tools/thinking 组装摘要请求
        ↓
首次使用完整历史，不预先砍断
        ↓
校验 <summary> 闭合、非工具调用、非截断、确实缩短
        ↓
保存完整原始转录
        ↓
再次校验前缀未变化
        ↓
原子替换主历史前缀
        ↓
更新 SDK，保留压缩后的尾部消息
```

失败、取消、磁盘错误、前缀竞争都不修改原上下文。

### 6.3 超限恢复

第一次始终使用全量历史。

只有明确的上下文容量错误才进入有损恢复：

- 使用最近完整消息的后缀；
- 比例依次为 0.7、0.5、0.35；
- 不截半条消息；
- 不拆媒体；
- 去掉开头孤立 tool result；
- 保持 system/tools 和消息原文。

摘要请求最多三次尝试，不因为普通 bad request 随意裁历史。

### 6.4 进度恢复

当前源码在摘要生成后，直接从 `TodoStore` 读取实时清单并确定性追加：

```python
text += "\n\n## 待办清单\n" + ...
```

位置：`src/xana/harness_sdk/default.py:938`。

因此 TODO 的权威不依赖模型是否主动把清单写进摘要。

---

## 7. 记忆与状态分层

Xana 中“记忆”不是一份文件，而是多层状态。

### 7.1 当前活动上下文

`ContextSDK`：

- 当前模型可见消息的运行时载体；
- 提供 snapshot/append/replace/restore/compact；
- 对嵌套 dict/list 做隔离，防止请求组装反向修改历史；
- 是持久主日志的可重建视图。

### 7.2 持久对话历史

`HistoryMirror`：

- JSONL 主日志；
- 正常写入顺序是**先持久化，再更新 SDK**；
- 启动时回放重建活动上下文；
- 后端提交后报错时重新读取主日志校准视图。

另外，`RunJsonlWriter` 保存面向审计和 Web 展示的完整 run 历史。压缩主上下文不会删除这些原始执行证据。

### 7.3 项目长期记忆

项目 `memory/**/*.md` 是唯一事实来源：

- 一条记忆一个 Markdown 文件；
- frontmatter 记录 name、description、type、topic、pinned；
- 每轮注入索引和置顶记忆；
- 相关记忆通过 `ProjectMemoryInjector` 召回全文；
- `memory_write` 是模型侧唯一写入口；
- Write/Edit 对 memory 目录受保护；
- 每轮结束后还有后台记忆提取补充遗漏。

SQLite 和搜索索引都是可重建派生视图，不是第二份事实来源。

### 7.4 知识树

项目 `README_ai.md`、`doc_ai/` 等构成知识树：

- 用于项目知识和设计文档检索；
- 与跨会话项目记忆分开；
- 不让任意项目文档自动成为长期记忆。

### 7.5 SAG2 / JEV

当前源码已经包含：

- SAG2 分词、BM25、FTS5、扩展检索；
- `ProjectMemorySearch`；
- 来源哈希和项目索引所有者校验；
- `EvidenceJudge` 契约；
- JEV Noul HTTP 适配器。

通过 `memory.recall="sag2"` 和显式 `memory.judge` 配置，`boot()` 可将自动 recall 切换到这条路径；本地 Karpo 与远端 JEV 共用 Noul 评分契约，评分失败不静默回退旧 selector。

---

## 8. 工具体系

### 8.1 统一 Tool 契约

每个工具实现：

```python
spec()
validate()
check_permission()
pre_tool_use()
execute()
post_tool_use()
render_result()
cancel()
```

调用链：

```text
工具解析
 → validate
 → check_permission
 → extension approval review
 → pre_tool_use
 → execute
 → post_tool_use
 → render_result
 → 写入上下文与历史
```

工具查找顺序：

1. extension 工具；
2. 共享 ToolRegistry；
3. 找不到则 `not_found`。

### 8.2 工具范围

`AgentSpec.tools/disallowed_tools` 支持：

- `default`
- 工具组名
- 具体工具名
- `*`
- 空集合

子 agent 再与父级 `tool_ceiling` 取交集，委派不能扩大权限。

schema 可见性和实际执行都会检查，不是只靠把工具从 prompt 中隐藏。

### 8.3 延迟披露

`ToolSpec.defer=True` 时：

- 模型先只看到名字和描述；
- 通过 `tool_search` 获取完整 JSON Schema；
- revealed tools 写入历史 metadata；
- 后续请求恢复已揭示状态；
- 没有 `tool_search` 的 agent 不隐藏工具。

这样减少大量工具的 schema 常驻开销，同时保持调用契约一致。

### 8.4 取消与副作用

- 只读工具：取消后立即终止；
- 写工具：先给约 2 秒完成宽限；
- 未完成再强杀并写入 synthetic warning；
- Bash 支持进程组 SIGTERM → SIGKILL；
- 恢复时不自动重放未知结果的写操作。

Grep 在独立工作进程执行：

- 30 秒超时；
- 256 MiB 内存限制；
- 最大 8 MiB/文件；
- 250 命中、48K 输出上限。

---

## 9. Agent 与 Skill

### 9.1 AgentSpec

`AgentSpec` 是声明式 frozen dataclass，描述：

- 身份；
- prompt；
- tools/skills；
- model/effort；
- max_turns；
- verify；
- 目标契约。

`FileAgentRegistry` 加载：

```text
~/.xana/agents/<name>/AGENT.md
<project>/.xana/agents/<name>/AGENT.md
```

项目级覆盖用户级，文件变化支持热加载。

### 9.2 子 Agent

`agent_run` 经 `AgentRunner`：

- 创建独立 session；
- 使用同一个 Gateway 和 DefaultHarness；
- 继承父级工具上限；
- 支持同步等待和后台执行；
- 可以查看子会话的实际 prompt、工具调用和结果；
- 不是另写一个简化执行循环。

### 9.3 Skill 两阶段加载

1. 启动时只提供可见技能列表；
2. 模型通过 `skill_load` 加载完整正文。

技能安装与启用分离：

- 安装只负责落地技能包；
- enable 明确指定 agents；
- `skill_bindings.json` 记录启用范围；
- 重启后保留；
- 安装过程不自动执行技能包脚本；
- references 等资源保留，相对路径以真实技能目录为准。

Agent 是角色定义，Skill 是可复用方法库，两者共用 Markdown 解析器但不共用生命周期。

---

## 10. Goal、TODO 与后台任务

### 10.1 GoalService

Goal 是独立持久状态，不是 system prompt 中的一句目标。

状态：

```text
active / paused / blocked / complete
```

保存：

- objective；
- completion criterion；
- token budget；
- tokens/steps/elapsed；
- completion_requested；
- pause/block 原因。

续跑复用当前模型/工具循环，不启动第二套 goal agent。

模型请求 complete 后：

1. 先设置 completion_requested；
2. 执行 AgentSpec.verify；
3. 验证通过才持久化为 complete。

服务重启时，活动 goal 规范化为 paused，避免自动重放副作用。

### 10.2 BackgroundTasks

适用于 agent_run 和 eval_run：

- 独立任务记录和结果；
- `background.json` 持久化；
- `task_wait` 只观察，不确认交付；
- `task_stop` 真正停止；
- 任务完成后以稳定 task ID 通知父会话。

结果交付流程：

```text
任务完成
 → 写主历史或持久输入队列
 → 记录 input/task 身份回执
 → 确认结果已交付
```

这样避免查询一次就丢结果、重启后重复注入、父 run 已结束就无法通知，以及取消父回答后自动复活。

---

## 11. MCP 子系统

### 11.1 核心原则

外部工具定义不是可信提示词；每次发现都必须经过审核才能进入共享工具目录。

统一管线：

```text
connect
 → tools/list 完整分页
 → 结构与注入审核
 → 用户可用性过滤
 → 注册到共享 ToolRegistry
```

触发点包括：

- 初次连接；
- 配置热加载；
- 手动 reload；
- 自动重连；
- `tools/list_changed`。

### 11.2 审核内容

确定性扫描：

- 名称、描述；
- input/output schema 的键和所有字符串；
- 忽略先前指令、伪造 system/developer；
- 泄露凭据或系统提示；
- 绕过审批、隐藏操作；
- 零宽和 bidi 控制符；
- schema 类型、大小、描述长度和工具数量限制。

审核失败按工具隔离，不影响同 server 的其他工具。

日志只保存 rule ID、JSON path、指纹、generation 和决定，不保存完整恶意 schema 或秘密。

### 11.3 状态机与 generation

Server 状态：

```text
connecting / connected / retry_waiting /
disabled / failed / removed
```

连接故障时：

1. 立即使 generation 失效；
2. 注销该 server 全部工具；
3. 2–60 秒指数退避；
4. 恢复后重新 list 和审核；
5. 再发布新 generation 工具。

旧 generation 回调不能重新上架。

传输失败的调用不自动重放，避免重复执行非幂等操作。

### 11.4 用户选择

用户 override 与连接定义分离：

- enabled；
- enabled_tools；
- disabled_tools；
- deferred。

更新采用字段级合并：

- 未提供的字段保留；
- 显式 null 清除；
- 显式 `[]` 表示空选择。

因此服务器开关不会覆盖用户之前的逐工具禁用选择。

---

## 12. 权限与安全

`PermissionService` 是唯一权限服务。

三档：

- `manual`：非只读操作通常需要审批；
- `yolo`：常规操作自动执行，高风险命令仍可审批；
- `auto`：自动执行，但硬禁止规则仍有效。

其他机制：

- plan mode 拒绝非只读工具；
- 子会话权限不能高于父会话；
- 会话级批准使用调用指纹；
- 渠道用户通过 PairingStore 授权；
- Web 使用 Bearer + Origin 检查；
- Unix socket 使用文件权限；
- Kimi token 0600、tmp+replace 原子保存；
- 媒体按 SHA-256 保存不可变快照；
- memory 和 agent 文档目录有专用写入门禁。

文件工具的 openat2 双模式目前只有设计文档，尚不属于运行代码能力。

---

## 13. 自进化与评测

当前源码中的 evolution 主要是：

```text
evolution/
├── contracts.py
├── ledger.py
├── episode.py
└── evals.py
```

### 13.1 EvalSuite

项目评测目录：

```text
.xana/evals/<suite>/
├── suite.yaml
├── METRICS.md
├── cases.jsonl
└── batches/
```

样本 split：train、heldout、live。

实现要点：

- 每个样本独立 session；
- 确定性 scorer，不依赖模型打分；
- 套件、样本、真值相关文件计算 fingerprint；
- agent 版本也计算 fingerprint；
- 同版本同套件可复用已运行结果；
- 支持日 token 预算和模型额度等待；
- heldout run 对迭代团队隐藏；
- 工具环境支持 as_of、replay、live、deny、simulate。

### 13.2 EpisodeRunner

状态机：

```text
declared → running → landing → closing → closed
```

预算设计：

- 从历史账本估计收尾 token，使用 P75；
- 无历史默认预留 20K；
- 达到 `budget - closure_estimate` 进入 landing；
- landing 拒绝新分叉、新 spawn、新评估；
- 在途工作允许收尾；
- 硬上限到达后进入 closing。

关闭时：

1. 执行 canary，确认迭代后 Agent 仍可用；
2. 比较同 case 集合相对 base 的增益；
3. canary 失败或无正增益则回滚；
4. 回滚 declare 时保存的 Agent 文档快照；
5. 写入 append-only Ledger；
6. 保存 continuation 和 branch lineage。

这套机制的重点是：迭代可靠性由状态机、预算、评估和回滚保证，而不是要求模型自觉遵守流程。

当前源码没有 `MetaEvolutionService` 或 `ChangeWriter` 文件；不要按旧文档把它们记成现行实现。

---

## 14. 模型与渠道适配

### 14.1 Provider

统一：

```text
ModelRequest → ModelClient.stream → ModelEvent
```

Provider 负责：

- 协议转换；
- reasoning/tool call 回传；
- usage；
- 模型能力；
- 鉴权和并发。

Provider 不负责工具执行、记忆、run 状态或 Agent 决策。

Kimi：

- OAuth device flow；
- access token 自动刷新；
- 401 强制刷新后重试一次；
- OpenAI chat-completions 方言；
- reasoning_content 和 tool_calls 回传；
- 默认 coding 模型与大上下文配置。

DeepSeek：

- Anthropic messages 方言；
- thinking block；
- 相邻 tool result 合并到同一 user turn。

OpenAI 兼容自部署模型使用 `type="openai_compatible"`，以静态 API key、标准 `max_tokens` 和 `image_url` 图片输入接入；默认不改变 Kimi 模型选择。

Fake 必须显式配置，不会在真实配置失败时静默启用。

### 14.2 Agent Adapter

渠道只负责：

- 外部消息转换；
- 会话绑定；
- 事件渲染；
- 卡片和按钮；
- 渠道配对。

渠道不维护独立 run 状态机、独立模型上下文、独立权限系统或独立 goal/todo 实现。

---

## 15. Web 工作台

Web 是运行状态的投影，不是第二套 runtime。

主要能力：

- 主/子会话查询；
- run 历史分页；
- SSE 活动事件；
- queue/steer/cancel；
- 三档权限和审批；
- Goal/Todo；
- 后台任务和子会话；
- 完整命令日志；
- 媒体上传；
- MCP 管理。

历史来源：

- 展示历史读取持久 run JSONL；
- 模型上下文由 ContextSDK 管理；
- 两者是不同投影，不相互覆盖。

断线恢复：

- 随机 epoch + 序号 cursor；
- 有效 cursor 回放；
- 重启或游标失效则重新 snapshot；
- 按 row_id 合并，避免重复。

---

## 16. 最值得记住的设计取舍

1. **单会话单循环**：解决并发回答、输入覆盖和取消混乱；queue/steer 都进入同一持久状态机。
2. **执行与观察分离**：页面、CLI 和聊天渠道只是观察者，断开不会停止长任务。
3. **持久历史与活动上下文分离**：主日志可重建 SDK；压缩和恢复不依赖模型记住所有细节。
4. **静态 system + 追加式 ContextNote**：兼顾动态信息和 prompt prefix/cache 稳定。
5. **统一工具执行链**：内置工具、扩展工具、MCP 工具都复用权限、取消、结果和历史语义。
6. **Markdown 原件 + 可重建索引**：避免数据库和文件双事实来源，迁移与分叉更简单。
7. **状态独立于提示词**：Goal、Todo、BackgroundTasks、Episode 都有正式持久状态，不靠模型自觉维护。
8. **用工程机制保证迭代可靠性**：版本指纹、heldout、canary、预算收尾和文档回滚构成闭环。

---

## 17. 完整项目介绍模板

Xana 是一个面向长程任务的 Agent Harness。它以 project 为隔离单位，每个项目拥有独立的 workspace、记忆、Agent 配置、会话和执行服务。核心采用单会话单执行循环，通过持久 queue 和显式 steer 处理插话，避免多 run 并发修改上下文。

上下文方面，静态 system 在会话内固定，动态信息以只追加的 ContextNote 注入，从而保持 prompt 前缀稳定。压缩由 ContextSDK 统一负责，包含前缀冻结、完整转录保存、原子替换和重启恢复，TODO 等任务状态从独立持久服务回填。

工具方面，内置工具、文件工具、扩展和 MCP 都进入同一个 ToolRegistry 与权限执行链。MCP 额外加入每次发现后的安全粗审、generation 失效、热加载和自动重连。

长任务方面，Goal、Todo、后台任务和运行记录都有独立持久状态，支持取消、恢复和结果去重交付。迭代方面，通过 EvalSuite 指纹、heldout、episode 预算状态机、canary 和文档快照回滚，让 Agent 的改进可以验证和撤回。

整体设计的重点不是增加更多模型指令，而是把长程任务需要的记忆、状态、权限、恢复和评测落实到明确的工程契约中。
