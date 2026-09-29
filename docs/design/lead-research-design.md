---
title: deep_research(主模型负责人 + 并行子 agent)设计
status: 已实施(2026-09-26)
date: 2026-09-26
---

# deep_research:主模型负责人 + 并行子 agent

## 0. 依据

2026-09-26 同题对比(同一研究问题,medium 档;报告在 scratchpad/compare):

| | A 搜索服务(27B 规划写作 + 4B 阅读) | B Kimi 负责人 + 4 个子 agent |
|---|---|---|
| 耗时 | 439 秒 | 920 秒 |
| 检索 | 22 查询、64 页(成功 37) | 43 次搜索、27 次打开(57 页) |
| 报告 | 14.7K 字、18 个来源 | 32.7K 字、79 个来源(35 个读过全文) |
| 深度 | 每家公司 1–2 句,来源质量混杂 | 每家有收入/订单/风险,发现口径矛盾(如"唯一代理" vs "代理商之一") |

B 明显更好,形态同 Anthropic 多智能体研究系统(lead + subagents)。A 保留为不消耗主模型 token 的后台通道。

## 1. 工作流

1. **规划**(主模型一次调用,无工具):把研究问题拆成 N 个互不重叠的子课题,每个写清要查什么、要什么证据、交付什么。N 由 effort 决定:low 3、medium 4、high 6。
2. **并行子 agent**:每个子课题一个 `researcher` agent 的独立 run(完整 harness,工具只有 web_search / web_open_url / Read,研究员身份,max_turns 20,无用户交互),交回带出处的发现。
3. **汇总**(主模型一次调用,无工具):只用发现里有出处的事实写报告,保留 `[标题](url)`,矛盾并列说明,列出未能确认的内容。
4. 进度:`report_progress` 上报"规划子课题"→"子研究员 k/N 完成 · 搜索 x 次 · 打开 y 个网页"→"汇总报告"。
5. 返回:报告正文 + 统计;子 agent 的原始发现写进工作区文件,路径附在结果里,供回查。

子 agent 的 run 通过 `parent_run_id` 挂在当前 run 下(已有机制:不注入无进度提醒、RunRecord 记父子关系)。

## 2. 需要新增 / 修改的部分

| # | 内容 | 位置 | 性质 |
|---|---|---|---|
| 1 | **`AgentRunner` ABC**:`async run(*, agent_name, prompt, session_id, parent_run_id, abort) -> AgentRunOutcome(run_id, status, text, usage)`,同步等到子 agent 结束并取回最终文本 | `extension_api/capabilities.py`(与 AgentSpawner 同处) | **新增 ABC,需确认** |
| 2 | `GatewayAgentRunner`:直接调 `Agent.send()` 收集 `final`,不走 `_submit`,不触碰渠道(不发 run_started/卡片、不 detach 前台);沿用 EpisodeGated 门禁与 DelegationPolicy 深度检查 | `agent_adapter/gateway.py` | 实现 |
| 3 | **harness 按 `AgentSpec.allowed_tools` / `disallowed_tools` 过滤工具**(schema 和执行两处):空 = 不限制(chat 现状);非空 = 白名单 | `harness_sdk/default.py`(经 config_snapshot 传入) | **现有字段首次生效;现有 agent 都是空列表,不影响** |
| 4 | 内置 `researcher` agent:`AgentSpec`(身份、allowed_tools=web_search/web_open_url/read、max_turns=20、无人值守 profile 但不开 goal 模式)在 boot 注册,与 chat 同一工厂 | `agents/researcher.py` + `boot.py` | 新增 |
| 5 | `LeadResearchTool`:构造注入 `AgentRunner`、`ModelRegistry`、provider/model;规划 → gather 子 agent → 汇总 | `tools/builtin/lead_research.py` | 新增 |
| 6 | 工具命名:新工具叫 `deep_research`;xana-search 服务的深度研究改名 `background_research`(后台任务、不耗主模型 token) | `xana_search.py` / `boot.py` | **改名,需确认** |

防递归:researcher 的白名单里没有 deep_research;AgentRunner 再按 DelegationPolicy.max_depth(默认 1)兜底。

取消:工具被取消时(用户中断),取消 gather 中的子 run(传 abort)。

## 3. 测试

- 单测:AgentRunner 取回最终文本、不触碰 adapter;工具白名单过滤(schema 与执行);LeadResearchTool 用假模型 + 假 runner 验证规划 / 并行 / 汇总 / 进度 / 取消。
- 实测:用同一问题跑新 deep_research,与 §0 的 B 对齐(质量、耗时),再在飞书里走一遍。

## 4. 决定(2026-09-26 确认)与实现位置

三项均已确认。同时做了 harness 并行执行工具调用(`harness_sdk/tool_batches.py`,连续的 parallel 调用成批并发,
上限 10,按发出顺序写回)与工具说明里的并行用法提示(read / web_search / web_open_url)。

- `extension_api/capabilities.py` `AgentRunner`;`extension_api/contracts.py` `AgentRunOutcome`
- `agent_adapter/agent_runner.py` `GatewayAgentRunner`(直接 `Agent.send`,取消即触发子 run 的 AbortSignal)
- `harness_sdk/default.py` `_tool_allowed`(schema 与执行两处);`ChatAgent` 把 spec 的白名单 / 黑名单放进 config_snapshot
- `agents/researcher.py`(内置子研究员 spec);`boot.py` 在配置了 xana-search 时注册 researcher 与 deep_research
- `tools/builtin/lead_research.py` `LeadResearchTool`;子研究员原始发现写到工作区 `research/<日期>/`
- `tools/builtin/xana_search.py` 服务端研究改名 `background_research`
- 测试:`tests/test_lead_research.py`、`tests/test_parallel_tools.py`

## 进度展示:中间思考与检索汇总(2026-09-29)

deep_research 从搜索服务挪到框架自身后,子研究员的每一步都在本进程里发生,进度可以细到"谁在想什么、搜了什么、读了什么"。

- **结构化进度**:工具除了一行 `report_progress(text)`,还可以 `report_activity(ToolActivity)`:`lane`(分道,如 `plan` / `r1` / `synth`)、`lane_title`(分道标题)、`kind`(thinking / search / open / tool / done / error / note)、`text`。harness 以 `tool_progress` 事件发出(payload 带 `activity`),卡片把同一工具调用的活动按分道累积。
- **子 agent 步骤**:`AgentRunner.run` 的回调从"只报工具名"改为报 `SubagentStep`:thinking(一段思考结束时,截取开头)、tool(工具名 + 按工具显示配置取的参数摘要,如搜索词、网址)、answer(最终回答)。deep_research 与 agent_run 共用。
- **deep_research 的分道**:
  - 规划:负责人的思考摘要 + 拆出的各子课题(标题 — 任务要点);
  - 每个子研究员一个分道:💭 思考摘要、🔍 搜索词、📄 打开的网页、✅ 完成时的统计与发现首句;
  - 汇总:负责人的思考摘要、撰写报告、完成。
- **展示**:进行中的一行状态显示最新一条活动;展开面板里是完整时间线(detailed 模式在该工具的面板里,grouped / summary 模式追加一个"过程"面板)。时间线按分道裁剪(每道保留最近若干行,总长受限),避免飞书卡片超出大小上限。回答结束后时间线保留,可随时展开回看。
