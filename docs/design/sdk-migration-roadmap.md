---
title: Xana 面向 RSI 的模块化迁移路线图
status: active
date: 2026-11-23
---

# Xana 面向 RSI 的模块化迁移路线图

> **文档定位**：跨阶段唯一主控路线图。只规定迁移顺序、阶段职责、单阶段工作流、准出标准和落地记录；不预先锁定任何尚未调研模块的内部接口。每完成一个阶段，以实际代码和正式模块文档为准推进下一阶段。
>
> **弹性条款**：本文的模块划分、阶段顺序和各阶段"起点线索"是可推翻的初始方向，不是施工清单。某阶段调研如有充分证据，可以提出合并、拆分或调整阶段，经确认后更新本文。各阶段内部如何设计，由该阶段自己的调研决定。

## 1. 目标

1. **面向 RSI 可迭代**：每个子系统能单独运行、单独评测、单独迭代。工程能力看性能、边界、并发和稳定性；算法/策略能力看准确率、召回率、收益和 token/延迟成本。
2. **可拼接**：任一子系统的某个版本，能与其他子系统的任意兼容版本组合运行；也能通过薄 adapter 接入外部 harness，或被其他实现整体替换。
3. **模块化、SDK 化、接口化、规约化**：区分"模块对外提供的接口"和"模块对宿主的要求"（注入点、生命周期钩子、隔离级别）。提示词注入等宿主要求本身也是规约的一部分，像配置一样有明确的注入点和拼装规则。
4. **零丢失、零残留**：现有功能全部保留；每个阶段同步迁完调用方并删除旧路径，不保留长期并存的兼容层。
5. **规约稳定但不过度预留**：规约定型后尽量不变、可前后兼容；只为已证实的需求设计，新需求出现时再补充。

## 2. 迁移原则

- **自底向上、叶子先行**：先把依赖最少的子系统独立出来，最后收口 harness 主循环。原因：在任何子系统独立跑通之前先冻结 harness 扩展协议，会把旧单体的隐性假设写进新接口；而从最厚的耦合处（DefaultHarness、Memory）先切，会逼出大量临时胶水。
- **叶子阶段只动接缝**：每个叶子阶段负责把"本模块与 DefaultHarness 的耦合"换成薄调用并删除旧代码，不重构主循环。主循环、会话上下文和策略槽在 Stage 6 收口。
- **两类规约分开管理**：
  - 模块**对外提供的接口**：在本阶段定型为稳定。
  - 模块**对宿主的要求**：在本阶段落到当前宿主上，并登记到"宿主需求账本"（见各阶段落地记录），状态为"暂定"。Stage 6 汇总所有阶段的需求后再统一定型。
- **契约 = 接口文档 + 一致性测试**：任何实现跑通同一套一致性测试，即视为可拼接。每个模块带契约版本；运行时把各模块及策略的版本写入轨迹。完整的版本兼容矩阵等出现第二个版本时再建。
- **冻结骨架与可进化策略分开**：稳定的数据结构与生命周期协议（带 `schema_version`，少动）与可被频繁替换的策略实现（带 `policy_version`，写入轨迹）分开。
- **迁移与新功能分开**：迁移阶段只保证现有功能等价；新能力（如 Dream、出错反思、新沙箱后端）在模块抽出并通过回归之后，作为单独的步骤开发和验收。
- **规约演化方式**：跨模块数据保持纯数据、可 JSON 序列化，不混入锁、回调、活服务；接口优先用单个请求/结果对象，迭代时优先追加带默认值的可选字段；持久格式变化在存储边界一次性迁移。

## 3. 文档制度

| 层级 | 位置 | 规则 |
|---|---|---|
| A 主控路线图 | `docs/design/sdk-migration-roadmap.md`（本文） | 全程保留；每阶段结束只更新状态与落地记录 |
| B 事实文档 | `doc_ai/<module>.md` 及其子文档；架构检查 `tests/test_arch_layers.py` | 只在代码迁完、旧代码删净、测试全绿后原位更新；只写已实现的事实：公开接口、版本规则、轨迹字段、独立运行/评测/调试入口。不另建平行目录 |
| C 阶段临时区 | `docs/_wip/stage_XX_<name>/` | 调研笔记、方案草稿、交接记录、Superpowers 生成的 spec/plan 都放这里；阶段结束前整目录删除。不放在 `doc_ai/` 下，因为 `xana sync` 会把 `doc_ai/` 同步进运行态记忆 |
| 资料缓存 | `~/project/best_agent/paper/`（仓库外） | 只存下载的论文、技术文章原文，跨阶段保留，避免重复下载。不存任何分析、摘要或结论；每个阶段必须针对本模块的目标重新阅读和分析 |

- 每个阶段同时清理本模块已过时的旧设计文档：列出清单，确认后删除或移出默认阅读路径。
- 单阶段如超出上下文预算（见 AGENTS.md 规则 1），拆成子阶段，交接记录放 C 层。

## 4. 单阶段工作流

**Step 1 调研**（新开上下文，只读本文、已完成阶段的 B 层文档和当前代码）需回答：
1. 本模块现有功能清单（引用 Stage 0 基线）和当前耦合点、前序阶段留下的妥协。
2. 能力边界：独立存在时的最小完备能力集，哪些东西不该放进来。
3. 对外接口，以及如何支持不同版本互相拼接。
4. 对宿主的要求：需要哪些注入点、钩子、隔离级别。
5. 轨迹与日志：要记录哪些输入、输出、中间状态、耗时/用量、版本指纹；如何从轨迹切片重放调试。
6. 评测：工程指标与算法指标分别是什么、数据集和条件；哪些是冻结骨架、哪些是可进化策略；迭代时如何防止过拟合。
7. 一致性测试范围，以及如何用第二个宿主（最小假循环）验证可拼接。
8. 业界怎么做：重点看模块怎么拆、怎么评测、哪些地方被迭代，不照搬实现（参考优先级见第 8 节）。

**Step 2 方案**：在 C 层输出本模块的契约设计、从最小切片到全量替换的步骤、调用方清单、数据迁移、旧入口删除清单、测试与评测矩阵、需要确认的行为变化。经确认后动工。涉及模型提示词时，遵守 AGENTS.md 的提示词来源约束。

**Step 3 迁移**：先用一个最小真实切片跑通新契约，再替换全部调用点（对尚未重构的 DefaultHarness 用薄调用接入），同时删除旧实现、旧接口和兼容分支；在架构检查中固化新的依赖边界。

**Step 4 验证**（删除旧代码后执行）：
- 独立运行：不启动完整宿主，通过模块自己的入口跑通功能、故障注入和评测集。
- 可拼接：同一契约下至少两个实现（如基线与替代/模拟实现）都通过一致性测试；第二宿主能接入；能从轨迹切片离线重放。
- 回归：全量既有测试与 `-m merge_gate` 通过。
- 真实渠道端到端（必选）：在运行中的 `xana serve` 上（如需 `xana sync`/重启，先单独检查所有项目的活动 run 并经用户确认），通过真实渠道（飞书 / Web / `xanac`）使用默认模型 Kimi 2.8 发起至少一次能触发本模块的真实聊天或请求，**观察并确认本模块在真实链路中被实际调用**（以日志、事件、轨迹或产物为证；测试后清理写入 `workspace/memory/` 等处的测试副产物）。若该阶段为尚未接入主循环的纯底座，则至少跑一次真实渠道问候/对话确认现有链路未受损，并明确记录模块真实调用的后续接入阶段。

**Step 5 落地文档**：更新 B 层事实文档；在本文对应阶段填写落地记录（实际入口、宿主需求、真实妥协）。

**Step 6 收尾**：需要部署时，先单独检查所有项目的活动 run，再执行 `xana sync`；删除 C 层目录；主仓与 `tests/` 仓分别提交（不 push），打阶段 tag。

### 用 Superpowers 驱动

每个阶段都用 Superpowers 技能驱动执行（技能位于 `~/.gemini/config/plugins/superpowers/skills/`，开工前先读 `using-superpowers`）。

| 步骤 | 使用的技能 |
|---|---|
| Step 1 调研 | `dispatching-parallel-agents`：现有代码与功能盘点、工程实现、论文等相互独立的调研并行派发，汇总写入 C 层 |
| Step 2 方案 | `brainstorming`：与用户对齐本阶段设计并确认；`writing-plans`：写出可逐项执行的迁移计划 |
| Step 3 迁移 | `subagent-driven-development`（首选）或 `executing-plans`；每项任务按 `test-driven-development` 执行；遇到失败用 `systematic-debugging` |
| Step 4 验证 | `verification-before-completion`：任何"完成/通过"结论先有命令输出为证；`requesting-code-review` 与 `receiving-code-review`：阶段性审阅 |
| Step 6 收尾 | `finishing-a-development-branch`：只用本地合并或保留分支 |

与 Superpowers 默认行为不同的约定（优先级：用户指令与 AGENTS.md > 本路线图 > 技能默认值）：
- spec 和 plan 写入 `docs/_wip/stage_XX_<name>/`，不写 `docs/superpowers/specs|plans/`；中间文档不进阶段最终提交。
- 不 push、不建 PR；`finishing-a-development-branch` 中的推送选项不使用。
- `tests/` 是独立 git 仓，主仓 worktree 不会带上它。默认在主仓新建阶段分支工作；如果要用 worktree，需同时为 `tests/` 仓建对应 worktree。
- 提交分两个仓分别进行；跑测试用 `uv run pytest`，同步依赖用 `uv sync --all-extras`。

## 5. 阶段总览

| 阶段 | 子系统 | 主要任务 | 对 DefaultHarness 的影响 |
|---|---|---|---|
| 0 | 基线与底座 | 功能清单与测试映射、包归属表、架构检查、最小数据原语、轨迹格式、评测记录格式 | 不动主循环 |
| 1 | LLM 驱动 | 多 Provider 收口；脚本化/回放/故障注入客户端；用量与缓存统计 | 替换直连模型调用和内嵌重试 |
| 2 | 执行环境与权限 | 执行隔离与权限裁决分开；只抽象到现有功能所需程度 | 抽走分散的权限判断 |
| 3 | 工具与观察管理 | 工具执行管线、并发与错误分级、输出裁剪/落盘/格式化、延迟披露、回放环境 | 抽走工具调度、`tool_search` 特判、截断逻辑 |
| 4 | Skills | 多源发现、两阶段加载、提案队列；评估 Todo/Goal 去向 | 抽走技能拼接与加载细节 |
| 5 | 长期记忆 | 先切出会话上下文；再独立长期记忆的存储、检索、写入 | 抽走所有 memory 耦合 |
| 6 | Harness 收口 | 汇总宿主需求定型扩展规约；会话上下文；策略槽；组合根 | 主循环重构 |
| 7 | 评测与迭代闭环 | 版本拼装对照评测、防过拟合晋升门禁、轨迹回放预筛、迭代账本 | 不改内核 |
| — | 宿主层 | gateway/渠道、Web、CLI、config、session_config、state、runtime（HTTP/调度/投递）、multimodal、publishing、benchmarks | 随各阶段调整调用方；是否进一步模块化由 Stage 0 归属表和后续调研决定 |
| — | 功能步骤 | Dream、出错反思、新沙箱后端等新能力 | 在对应模块迁移完成后单独进行 |

## 6. 各阶段要点

> 每阶段的"起点线索"只是调研入口，不是完整清单。

### Stage 0：基线与底座
**状态**：已完成（2026-11-24）

- **为什么先做**：没有功能清单，"零丢失"无法核对；没有统一轨迹和版本标识，每个模块会各写一套日志，后面无法做跨模块归因和回放。
- **起点线索**：
  - 现有功能清单 → 对应测试的映射（包括 `-m merge_gate`、前缀稳定性等契约锁）。
  - 全部包的归属表：每个包属于哪个阶段或宿主层。
  - 架构检查：单向依赖、独立导入、数据契约可序列化。
  - 最小数据原语：只收拢已经确定跨模块共享的消息、错误、用量结构，不定义任何业务接口。
  - 轨迹格式：append-only；组装清单（各模块与策略的名称、版本、配置哈希、种子）；`run/turn/span` 因果 ID；大载荷外置引用；评测记录格式（数据集版本、指标、条件）；最小查看/切片工具。
  - `componentization_iteration.md` 中已有的 tape 设想。
  - 清理会带偏后续调研的过时文档（列清单确认）。
- **准出**：纯数据结构与序列化/查看工具；架构检查生效；功能清单与归属表获确认；真实渠道端到端：通过飞书/Web/`xanac`（或隔离真实模型冒烟）完成至少一次真实问候/对话确认现有主链路未受损（`xana.protocol` 为纯底座，尚未接入运行态主循环，其真实调用随 Stage 1–7 逐一接入验收）。
- **落地记录**：
  - **入口与事实文档**：`src/xana/protocol/{versioning,primitives,tape,eval,__main__}.py`（stdlib-only，17 个公开 wire dataclass 受 `tests/golden/protocol_contract_v1.json` 锁定）；事实文档 `doc_ai/protocol.md`、`doc_ai/module_io_map.md`；已删死代码 `src/xana/contracts.py`（封禁于 `RETIRED_MODULES`）与误落 `src/xana/.ruff_cache/`；单循环旧迁移文档归档至 `docs/archive/single-loop-migration/`。
  - **测试基线**：全量 `uv run pytest -q` 1620 passed, 2 skipped；`-m merge_gate` 11 passed（旧记忆 39+1 为单循环重构前口径，后续以最新实测为准）；Stage 0 专项与门禁 78 passed；真实运行态验收通过（`xana setup --no-wizard` 同步代码并刷新 launcher 至 `.venv`，拉起 `default`/`demo`/`llm-papers` 全部 serve 且飞书 WS 在线，`xanac --project demo/default --random-session "你好"` 经 `kimi-for-coding` 正常回复，无多余记忆写入）。
  - **宿主需求账本**：Stage 0 无宿主要求（纯数据底座）。
  - **真实妥协与后续注意**：① 存量 2 个强连通环（`context <-> memory` 及 13 包巨环）共 124 条跨包边冻结在 `tests/test_arch_layers.py::ALLOWED_LEGACY_EDGES`（只减不增），`xana.protocol` 第一天即 0 违例；② 现有 `RunJsonlWriter` 与 `EvalRunner` 留待 Stage 6/7 原位切入新 Tape/Eval 协议，Stage 0 不搅动主循环；③ 3 条代码审阅 Minor 暂缓（`EMPTY_RUN_SUMMARY` 单例可变 dict 与 `eval.py` null 解析/原子写、`MigrationRegistry` 浅拷贝/重复源校验、少量补充边界单测），可在后续触及对应文件时顺带收口。

### Stage 1：LLM 驱动
**状态**：已完成（2026-11-25）

- **为什么排第一个业务模块**：它只依赖 Stage 0，又是工具摘要、记忆提取与召回、主循环、评测裁判的共同前置。脚本化与回放客户端立住以后，后面所有模块都能做确定性测试。
- **起点线索**：Kimi OAuth、DeepSeek、OpenAI 兼容、Fake 等 Provider；DefaultHarness 内的流式重试；`runtime/memory_extract.py`、`memory_judge.py` 的直连模型/评分调用。
- **调研重点**：协议层（流式组装、tool call 解析、结构化错误）与可进化请求策略（请求改写、超限降级、退避与自修复、路由）的分界；缓存命中、首字延迟、payload 哈希等记录；按请求指纹或顺序回放；常见流式/网络故障注入。
- **准出**：可脱离宿主单独发请求、压测流式解析、回放历史模型调用；脚本化客户端跑通异常恢复测试；真实渠道端到端：通过飞书/Web/`xanac` 发起至少一次真实聊天（Kimi 2.8），观察日志/轨迹确认新 LLM 驱动实际处理了流式请求并记录用量与缓存统计。
- **落地记录**：
  - **入口与事实文档**：`src/xana/llm_driver/{contracts,interfaces,errors,accumulator,policies,tape_recorder,profiles,driver,registry,testing,__main__}.py` + `auth/{kimi_oauth,kimi_media,resolvers}.py` + `transports/{openai_chat,anthropic_messages,dsml_recovery,stream_scrubber}.py`（10 个公开 dataclass 受 `tests/golden/llm_driver_contract_v1.json` 锁定）；独立 CLI `python -m xana.llm_driver <bench|replay|smoke>`；事实文档 `doc_ai/llm_provider_contract.md`、`doc_ai/module_io_map.md`；已原位删除 `src/xana/llm_driver/fake_client.py` 与整个 `src/xana/llm_driver/providers/` 目录并封禁于 `tests/test_arch_layers.py::RETIRED_MODULES`；彻底消除 `("llm_driver", "config")` 跨包边（124 → 123）；`DefaultHarness` 移除内联 `_STREAM_RETRIES` 循环，改消费 `ModelEvent(kind="retry")`；`compact.py` 移除字符串匹配，改消费结构化 `error.reason` / `should_compress`；`memory_extract.py`（`priority="background"`）与 `lead_research.py` 改用 `complete()`。
  - **测试基线**：全量 `uv run pytest -q` **1618 passed, 2 skipped, 0 failed**；`-m merge_gate` **11 passed**；独立 `pro` Code Review（2 Critical + 10 Important 全部经 TDD 修复并补齐 `tests/llm_driver/test_review_regressions.py`）；独立 CLI 验证：`bench` 3/3 pass（`pass_rate=1.0`）、`smoke --model kimi-for-coding --credentials ~/.xana/credentials/kimi.json --tape-dir ...` + 离线 `replay` 闭环通过（`cache_read_tokens=91`, `REPLAY_OK count=1`）；真实渠道端到端验证：`xana sync` 同步后确认 `~/.xana/code/xana/llm_driver/` 无旧路径残留，`default`/`demo`/`llm-papers` 全部重启且飞书 WS 在线，`xanac --project demo` 经 Kimi 2.8 完成思考流 + `Bash` 工具调用往返（`stage1-live-e2e-ok`），零错误日志、零多余记忆写入。
  - **宿主需求账本**：① `boot.py` 需向 `KimiTokenStore` 显式传入 `xana_home() / "credentials" / "kimi.json"`；② `DefaultHarness` 消费 `ModelEvent(kind="retry")` 时需清空当前尝试缓冲并转发进度事件；③ `runtime/memory_judge.py`（Karpo 4B `/v1/systemone` 非标准聊天协议）保持在 Stage 5（Memory）范围，不强塞进 LLM Driver。
  - **真实妥协与后续注意**：① 保留 `("llm_driver", "multimodal")` 跨包边用于 `MediaInput` 传递与 `KimiMedia` 上传，待 Stage 3/后续按需收敛；② `LlmTapeRecorder` 已在 `ModelDriver` 内打通 Stage 0 `TapeWriter`，但在主循环中默认以 no-op 运行，待 Stage 6（Harness 内核重构）统一把 run 级 `TapeWriter` 注入 `ModelDriver`；③ `RoutingStrategy` 目前在 `ModelDriver` 内支持同 profile 下的模型回退（跨 provider 路由留待 Stage 6 多客户端组合层接入）。

### Stage 2：执行环境与权限
**状态**：已完成（2026-11-25）

- **为什么在工具之前**：权限判断现在混在工具内部，`policy/interfaces.py` 为空。不先拆开，Stage 3 只能把权限和执行逻辑继续塞在工具或宿主里。
- **范围约束**：只抽象现有能力。openat2、容器、远端执行等新后端属于后续功能步骤。若调研认为本阶段更适合并入 Stage 3，可以合并。
- **起点线索**：`PermissionService` 三档模式、plan mode、子会话权限上限、审批与审批复核、Bash 进程组清理、memory/agent 文档写门禁、Grep 工作进程资源限制。
- **调研重点**：执行环境的最小统一抽象（含测试用假环境）；不可妥协的安全边界与可进化的风险分类策略如何分层；锁/租约/目录快照是否放在这一层；权限裁决与资源消耗的轨迹记录。
- **准出**：可脱离工具和 harness 独立运行安全测试集（放行率、高危拦截、超时与孤儿进程清理、越界防护）；真实渠道端到端：通过飞书/Web/`xanac` 发起至少一次触发权限裁决与执行边界的真实请求（如读文件、执行命令或受保护路径），观察日志/轨迹确认新执行环境与权限模块被实际调用。
- **落地记录**：
  - **入口与事实文档**：`src/xana/policy/{contracts,interfaces,approvals,shell,path_policy,exec_env,tape_recorder,testing,__main__}.py`（9 个公开 dataclass 受 `tests/golden/policy_contract_v1.json` 锁定）；宿主薄适配器 `src/xana/runtime/policy_adapter.py`（于 `boot.py` 注入）；独立 CLI `python -m xana.policy <bench|check>`；事实文档 `doc_ai/policy.md`、`doc_ai/module_io_map.md`；彻底消除 `("policy", "harness_sdk")`/`("policy", "session_config")`/`("policy", "state")`/`("policy", "tools")` 全部 4 条跨包边（123 → 119）；`Write`/`Edit` 改用统一 `ProtectedDirsPathPolicy`；内置可开关诊断日志 `DiagLogger("policy")` / `DiagLogger("env")`。
  - **测试基线**：Stage 2 专项 + 架构/权限/工具测试全绿；`python -m xana.policy bench` 5/5 pass（`pass_rate=1.0`）；全量回归与 `merge_gate` 全绿；真实运行态同步与 `xanac` 端到端验收通过。
  - **宿主需求账本**：① 宿主通过 `PolicyStatePort` 提供会话权限模式/授权/父会话查询，通过 `PolicyEventSink` 转发审批事件；② `DefaultHarness` 在 `_dispatch_tool` 构造 `AuthorizationRequest` 交 `PermissionService.authorize`。
  - **真实妥协与后续注意**：① `tools/builtin/_process.py::ProcessRunner` 保留流式 `on_output` 回调供工作台实时进程日志消费，同时接入 `DiagLogger("env")`，待 Stage 3 统一工具执行管线时评估合并流式接口；② `PolicyTapeRecorder` 默认 no-op，待 Stage 6 统一注入 run 级 `TapeWriter`。

### Stage 3：工具与观察管理
**状态**：已完成（2026-11-25）

- **为什么把观察管理放在这里**：工具输出如何过滤、截断、落盘、提取报错并转成模型可读内容，直接影响 token 效率和多步成功率，也是常被迭代的部分；它与工具执行高度内聚。
- **起点线索**：32+ 内置工具、10 类失败归因、只读即停与写操作宽限、并发批次、延迟披露 `tool_search`、MCP 审核/热加载/generation 状态机、派生工具、显示配置、`ToolEnvironment` 回放与时点截断。
- **调研重点**：声明式资源访问与并发冲突；模型、UI 进度、程序/评测三种输出分离；可由模型自纠的错误与必须中止的错误；外部数据污染标记；观察策略、工具披露策略、调用前校验等可替换策略；live/record/replay/simulate 环境模式。
- **准出**：任意工具和整条执行管线可通过独立入口喂入用例或轨迹切片运行和评测，不依赖 harness 或 memory；真实渠道端到端：通过飞书/Web/`xanac` 发起至少一次触发真实工具调用（含大输出裁剪或 `tool_search`/MCP 工具）的请求，观察日志/轨迹确认新工具执行管线与观察管理模块被实际调用。
- **落地记录**：
  - **入口与事实文档**：`src/xana/tools/{contracts,interfaces,batches,observation,disclosure,tape_recorder,__main__}.py`（新增纯 JSON 契约 `ToolObservation` / `DisclosureContext` / `DisclosurePlan` 受 `tests/golden/tools_contract_v1.json` 锁定；新增 2 个可进化策略 ABC `ObservationPolicy` / `ToolDisclosurePolicy`）；独立 CLI `python -m xana.tools bench`；已删除 `src/xana/harness_sdk/tool_batches.py` 并封禁于 `RETIRED_MODULES`；`DefaultHarness` 原位切入 `batches` / `DefaultObservationPolicy` / `DefaultToolDisclosurePolicy`；内置可开关诊断日志 `DiagLogger("tools")`（覆盖批次耗时、截断/失败异常）。
  - **测试基线**：Stage 3 专项 + 架构/并行/延迟披露/工具测试全绿；`python -m xana.tools bench` 4/4 pass（`pass_rate=1.0`）；全量回归与 `merge_gate` 全绿。
  - **宿主需求账本**：① `DefaultHarness` 向 `ToolDisclosurePolicy.plan()` 提供 `DisclosureContext`（含 `revealed_tools`、`available_tools`、`unattended`、`search_provider`）；② `DefaultHarness` 在 `tool.render_result` 后调用 `ObservationPolicy.normalize()` 统一执行输出截断与污染标记。
  - **真实妥协与后续注意**：① 32+ 内置工具到宿主服务（`skills`/`memory`/`agents`/`evolution`）的既有边留待 Stage 4–7 对应子系统独立时一并收敛；② `ToolTapeRecorder` 默认 no-op，待 Stage 6 统一注入 run 级 `TapeWriter`。

### Stage 4：Skills
**状态**：已完成（2026-11-25）

- **为什么在 Memory 之前**：Skills 结构轻、状态少，适合先跑通"业务模块只通过标准工具和增量上下文块挂到宿主"的方式；后续 Memory 产出的流程经验需要投递为技能提案，接收端先立住。
- **起点线索**：两阶段加载、项目级覆盖、热加载、`skill_bindings`、内置技能、安装与启用分离；TodoStore、GoalService 与内核的耦合。
- **调研重点**：多源发现与信任分级；技能目录摘要的 token 成本与前缀稳定；提案队列（来源、证据、审批、过期）；激活准确率与上下文开销的独立评测；Todo/Goal 是否同样作为扩展从内核剥离。
- **准出**：skills 不依赖 harness 和 memory；可独立评测匹配召回、注入格式和提案生命周期；真实渠道端到端：通过飞书/Web/`xanac` 发起至少一次命中技能目录并触发 `skill_load` 的真实请求，观察日志/轨迹确认新 Skills 模块被实际调用并注入上下文。
- **落地记录**：
  - **入口与事实文档**：`src/xana/skills/{contracts,interfaces,catalog,proposals,tape_recorder,__main__}.py`（新增纯 JSON 契约 `CatalogContext`/`CatalogEntry`/`CatalogPlan`/`SkillProposal` 受 `tests/golden/skills_contract_v1.json` 锁定；新增 2 个 ABC `SkillCatalogPolicy`/`SkillProposalQueue`；`InMemorySkillProposalQueue` + 原子持久 `FileSkillProposalQueue`）；独立 CLI `python -m xana.skills bench`；`SkillsListingContributor` 原位切入 `DefaultSkillCatalogPolicy`；`SkillLoadTool` 接入 `SkillTapeRecorder` 与可开关诊断日志 `DiagLogger("skills")`；零新增跨包边（`skills` 仅依赖 `protocol` 与 `component`）。
  - **测试基线**：Stage 4 专项 + 架构/技能测试全绿；`python -m xana.skills bench` 3/3 pass（`pass_rate=1.0`）；全量回归与 `merge_gate` 全绿。
  - **宿主需求账本**：① `SkillsListingContributor` 通过 `TurnInputContributor` 注入 `CatalogPlan.rendered`（保持 system 前缀逐字节稳定）；② Stage 5（Memory）产出的流程经验通过 `SkillProposalQueue.submit()` 投递，须经显式审批不自动激活。
  - **真实妥协与后续注意**：`TodoStore` / `GoalService` 的内核剥离归入 Stage 6（Harness 收口）统一处理，不打断 Stage 4 叶子边界。

### Stage 5：长期记忆
**状态**：已完成

- **为什么在这里切**：轨迹、LLM、执行环境、工具与污染标记、技能提案接收端都已就绪，Memory 可以作为完整独立的子系统抽出，同时卸掉 DefaultHarness 最重的一块耦合。
- **第一步**：把 `ActiveContextStore`、`HistoryMirror`、`MemoryReplayer` 等会话上下文移出 memory 包，归入 harness 上下文层（行为不变，Stage 6 再重构）。
- **起点线索**：`memory/**/*.md` 唯一事实来源、索引与置顶注入、**SAG2 + Karpo 4B (`karpo-decision-4B --graphs`)** 作为唯一检索与召回基线（旧知识树/FTS/`select`/`lexical` 已删除）、每轮后台提取、`memory_write` 门禁、项目隔离；`default.py` 与 `contracts.py` 中的 memory 相关耦合；`runtime/memory_extract.py` 对具体存储类的依赖。
- **调研重点**：支持快照/分叉/回滚的权威存储（评测时可隔离副本）；写入通道与污染回合处理；检索与提示策略的可替换槽位；固定时间点的离线评测集（召回、过时误用、冲突处理、自身成本）。
- **迁移完成后的功能步骤**：出错反思、Dream 整理（参考现有 `docs/design/memory-dream-design.md`），单独开发与验收。
- **准出**：memory 可脱离 harness 独立运行写入、提取、检索全链路评测；可替换为空实现或模拟实现而不影响主系统；真实渠道端到端：通过飞书/Web/`xanac` 发起至少一次触发记忆召回与写入/后台提取的真实对话，观察日志/轨迹确认新长期记忆模块被实际调用（验收后清理测试写入的记忆文件）。
- **落地记录**：完成 `src/xana/memory/{contracts,interfaces,store,recall,project_search,tape_recorder,testing,__main__}.py` 与 `runtime/{memory_judge,memory_extract}.py`、`tools/builtin/memory_write.py` 改造；新增 `MemoryStore`/`MemoryRetrievalPolicy`/`MemoryExtractionPolicy` ABC、`ProjectMemoryStore` 快照/分叉/回滚、`DefaultMemoryRetrievalPolicy`、`MemoryTapeRecorder`、`FakeEvidenceJudge`、`DiagLogger("memory")` 与 `python -m xana.memory bench`；锁定 `tests/golden/memory_contract_v1.json`；全量单测、`merge_gate` 与真实 `xanac --project demo`（SAG2 + Karpo 4B）端到端验收通过。会话上下文（`ActiveContextStore`/`HistoryMirror`）保留在 `memory` 至 Stage 6 收口时统一迁移，避免新增遗留边。

### Stage 6：Harness 收口
**状态**：已完成

- **任务**：
  - 基于各阶段宿主需求账本，定型扩展规约：注入点与拼装规则、生命周期钩子、子会话隔离（防止内部辅助任务递归触发）、配置切片、压缩前后钩子等，以账本实际需要为准。
  - 接管会话上下文与主日志。
  - 把剩余硬编码启发式抽成带版本的策略：提示词组装与动态块去重、压缩、回合控制与熔断、完成前验证等。
  - 安置 agents/子会话、后台任务、goal/todo（若前面未处理）。
  - 内核与组合根（`boot()`）分离。
- **必须保留**：单会话单循环（queue/steer/cancel/重启恢复）、提示词前缀逐字节稳定、原子压缩与 TODO 恢复、goal 与 verify 续跑、子 agent 权限上限、后台任务可靠交付。
- **准出**：内核仅凭脚本化模型和假工具跑完多轮压缩、续跑熔断、隔离、验证测试；增删 Skills 或替换 Memory 无需修改 harness；真实渠道端到端：通过飞书/Web/`xanac` 完成至少一次真实多轮对话（含工具调用及打断/取消或压缩场景），观察日志/轨迹确认收口后的 Harness 主循环、扩展组装与策略槽被实际调用。
- **落地记录**：完成 `src/xana/harness_sdk/{contracts,interfaces,policies,tape_recorder,default,__main__}.py` 与 `src/xana/context/sdk.py` 改造；新增 `CompactionPolicy`/`StopDecisionPolicy` ABC 及默认带版本实现 `DefaultCompactionPolicy`/`DefaultStopDecisionPolicy`、`HarnessTapeRecorder`、`DiagLogger("harness")` 与 `DiagLogger("context")`、独立运行入口 `python -m xana.harness_sdk bench`（脱离宿主用脚本化模型与假工具跑通多轮工具循环、压缩决策、续跑熔断与轨迹回放）；锁定 `tests/golden/harness_contract_v1.json`；全量单测、`merge_gate` 与真实 `xanac --project demo` 多轮工具调用端到端验收通过。

### Stage 7：评测与迭代闭环
**状态**：已完成

- **起点线索**：EvalSuite（train/heldout/live、指纹复用、确定性 scorer）、EpisodeRunner 状态机与预算收尾、Ledger、Harbor/τ² 适配、ToolEnvironment。
- **调研重点**：通过组装清单拼装任意模块版本，在固定条件下做配对对照；防过拟合（heldout 门禁、金丝雀、复杂度约束、自动回滚）；用历史轨迹切片低成本预筛候选；迭代账本（目标模块、故障聚类、假设、diff、评测条件、预期与实测、裁决），支持换基座模型时区分模型专属改动与通用改动。
- **准出**：至少一个单模块迭代闭环和一个多版本拼装回归闭环跑通，产物可追溯、可回滚；真实渠道端到端：通过飞书/Web/`xanac` 或生产同源入口触发至少一次真实任务与评测/迭代记录，观察日志/轨迹确认新评测、打分与账本/回滚链路被实际调用。
- **落地记录**：完成 `src/xana/evolution/{contracts,interfaces,promotion,tape_recorder,ledger,evals,__main__}.py` 改造；新增 `IterationLedgerStore`/`PromotionGate`/`BenchmarkRunner` ABC、`StatisticalPromotionGate`（配对 train/keep/heldout 防过拟合门禁）、`VersionAssembly`、`batch_to_benchmark_snapshot`（对接 Stage 0 `EvalTrialRecord`/`BenchmarkSnapshot`/`RunManifest`）、`EvolutionTapeRecorder`、`DiagLogger("eval")` 及独立运行入口 `python -m xana.evolution bench`；锁定 `tests/golden/evolution_contract_v1.json`；全量单测、`merge_gate` 与真实 `xanac --project demo` 端到端验收通过。

### 迁移收尾（全部阶段完成后）
**状态**：待启动

- 对照 Stage 0 功能清单做最终全量回归，覆盖全部渠道（Web/CLI/Feishu/TG）与真实模型端到端验收，确认全链路各迁移模块均被真实调用。
- 确认 `docs/_wip/` 已清空，无残留的过渡代码和旧文档。
- 删除 `AGENTS.md` 中"模块化迁移路线图"临时入口。
- 本路线图归档或删除；仍有效的长期约定（如轨迹格式、契约版本规则）先合入 `doc_ai/` 对应文档（改 `doc_ai` 前确认）。

## 7. 硬性约束

1. 未进入某模块的阶段，不为它预写接口；不跨阶段继承草稿。
2. 不保留长期双轨：最小切片验证 → 全部替换 → 立即删除旧代码。
3. 没有独立运行入口和轨迹回放，不算模块完成。
4. 冻结骨架与可进化策略分开，并带版本。
5. 如实记录妥协和宿主需求，作为下一阶段调研的事实基础。
6. 对照 Stage 0 功能清单，现有功能零丢失。
7. 每个阶段必须完成至少一次真实渠道（飞书 / Web / `xanac`）端到端验证，并观察到本迁移模块被实际调用（纯底座未接入主循环的阶段须验证现有主链路未受损并记录接入时机），否则不算阶段准出。
8. AGENTS.md 的项目规则全程适用（组合根唯一、提示词来源、真实模型默认 Kimi 2.8、改 `doc_ai` 前确认、不经要求不 push）。

## 8. 参考方向

**资料优先级**：
1. Xana 现有代码与功能清单；
2. 成熟工程实现与一线技术分享：Codex、Kimi Code、Claude Code 源码与文档；OpenAI harness engineering、Anthropic context engineering 等实践文章；MCP 规范；本地 `~/project/best_agent/` 下的 hermes-agent、openclaw、letta-code、mem0、MemOS、deepseek-harness、oh-my-pi、RD-Agent；
3. 论文。

**论文来源优先级**：
1. Anthropic、OpenAI、Google（含 DeepMind）；
2. 国内：Kimi（Moonshot）、DeepSeek、GLM（智谱）；
3. 其他机构与高校。

**论文的用法**：只用来找共性——迭代循环消费哪些信号、必须保留哪些原始轨迹、改动了哪些部位、如何防止过拟合——从而判断模块需要记录什么、预留什么。不照搬论文的架构和做法。可按需检索：Meta-Harness、Harnesses That Improve Themselves、ModularRSI、MESH-Harness、RRSI、Self-Harness、Self-Improvement Memory、Dream-RSI、AutoHarness、HyperAgents、ACE、Sleep-time Compute。

**跨阶段复用**：已下载的原文放在资料缓存中，后续阶段直接复用，不必重复下载；但前一阶段的分析结论不复用——每个模块的任务目标不同，必须重新分析。

## 9. 阶段启动模板

```text
我们正在按 docs/design/sdk-migration-roadmap.md 推进 Xana 的模块化迁移。
请只读取以下内容（不要读取历史草稿或依赖旧对话结论）：
1. 路线图：docs/design/sdk-migration-roadmap.md
2. 已完成阶段的事实文档（doc_ai/ 中对应模块文档）与 tests/test_arch_layers.py
3. Stage [X] 相关的现有 src/xana/ 源码

本轮只执行 Stage [X] 的 Step 1（调研），回答路线图第 4 节 Step 1 的问题。
按路线图第 4 节"用 Superpowers 驱动"执行，先读 using-superpowers。
资料按路线图第 8 节的优先级选取；~/project/best_agent/paper/ 中已有的原文可直接使用，但须针对本阶段目标重新分析。
路线图中本阶段的"起点线索"只是入口，如有证据可以提出调整范围或阶段划分。
调研报告写入 docs/_wip/stage_[X]_<name>/research.md，方案确认前不修改业务代码。
```
