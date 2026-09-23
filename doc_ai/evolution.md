---
title: evolution
type: doc
status: active
---

# evolution — 24h 自进化迭代框架（设计共识）

> 本文是 2026-09 设计讨论的落档，描述目标形态与远期方向。四个阶段的实现已全部落地，
> **实现真相见各 active 模块文档**（主要是 [evolution_module](evolution_module.md)），
> 本文不再复述机制细节；仅保留设计原则与仍未实现的远期方向。

## 定位

让 harness 在无人值守下长期运行并自我改进。**迭代对象 = Agent 文档**
（prompt/工作流/工具边界），不做参数级训练。核心命题：进化的可靠性由
工程机制兜底，不靠模型自觉。

## 已实现（见 active 模块文档）

| 设计共识 | 实现文档 |
|---|---|
| 记忆四层 L0–L3（唯一写入口、查重门禁、supersede 链） | [memory](memory.md) / [memory_rules_gate](memory_rules_gate.md) |
| 文件 Agent 文档（`~/.xana/agents/{name}/AGENT.md`）→ AgentSpec → ChatAgent | [agents](agents.md) / [agents_registry](agents_registry.md) |
| 迭代 episode 生命周期（declared→running→landing→closing→closed、软/硬阈值、closing 两阶段、僵尸强制闭合、文档冻结/回滚） | [evolution_module](evolution_module.md) |
| 变更单 schema（假设→diff 摘要→证据指针→判决）走 L2 门禁 | [evolution_module](evolution_module.md) / [memory_rules_gate](memory_rules_gate.md) |
| 元 agent 定时唤醒（cron 幂等安装、prompt 固定四段组装、held-out 不回流、propose_doc_change 唯一提案路径） | [evolution_module](evolution_module.md) / [runtime_scheduler](runtime_scheduler.md) |
| spawn/分叉门禁 + Write/Edit 受保护目录（L2 + ~/.xana/agents） | [agent_adapter_gateway](agent_adapter_gateway.md) / [tools_permission_sandbox](tools_permission_sandbox.md) |
| serve 侧 marker 任务 dispatch（`_make_scheduler_dispatch`） | [cli_serve](cli_serve.md) |
| harness usage 写点消费 episode 阈值（`episode_threshold` 事件、优雅落地、RunRecord.metadata `episode_exceeded`） | [harness_sdk](harness_sdk.md) / [evolution_module](evolution_module.md) |

## 设计原则（仍然有效）

- **唯一性是全局属性，由存储层机械执行**：唯一写入口 + 写时查重门禁 +
  supersede 取代链，模型只提交写入意图。
- **有边界的 episode**：迭代不是无休止任务，长跑的是框架，会死的是迭代；
  收尾预留（账本历史 P75）保证 closing 不是草草结束。
- **评估门禁**：冻结快照 → visible 集迭代用、held-out 选择用且分数不回流；
  Δ 相对 base（不是上一版）；>0 才保留，否则回滚。账本不允许无 verdict 的条目。
- **分叉 lineage**：分支结果只经账本/变更单回流，不做上下文物理合并（防分支污染）。

## 仍未实现（远期方向）

- **replay simulator**：L0 轨迹回放重评估，未实施。
- **后台 bash task 系统**：待 run registry 稳定后自然延伸，未实施。
- **着陆期 compaction 换保留策略**：landing 期仍用普通聊天式 compaction，
  面向收尾的交接 checkpoint / 保留策略未实施（见 [evolution_module](evolution_module.md)"未实施"）。
- 参数级训练、SandboxManager、成本换算（先用 token 预算）——明确不做。

## 相关模块

- [evolution_module](evolution_module.md) — 本框架的实现真相（阶段四落地）
- [memory](memory.md) — L0-L3 落地层
- [agents](agents.md) — Agent 文档/registry/spawn
- [runtime](runtime.md) — scheduler 唤醒、boot 组装
