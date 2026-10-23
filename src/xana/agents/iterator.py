"""内置迭代智能体(docs/design/iteration-agent-design.md §9):按固定流程改进一个有目标契约与评测套件的 agent。

提示词与被迭代的业务无关:业务知识只来自目标契约、评测套件的打分结果和运行记录。
有 Bash(读代码、跑分析脚本),但评测套件被改动后打分会被拒绝(EvalSuite.fingerprint),
目标契约只能经 agent_version 之外的人工途径修改。
"""
from __future__ import annotations

from xana.agents.contracts import AgentSpec
from xana.component.contracts import ComponentSource, ComponentSpec

ITERATOR_AGENT = "iterator"
ITERATOR_TOOLS = ("fs", "exec", "eval", "tool_define", "web_search", "web_open_url", "TodoWrite")
ITERATOR_DISALLOWED = ("Write", "Edit")   # 改 agent 只经 agent_version;分析结果写在回答和账本里

ITERATOR_IDENTITY = """你是迭代智能体:按固定流程改进一个已有的 agent(下称"被测 agent")。被测 agent 由人创建,frontmatter 里的目标契约(goal、success 指标、constraints、eval 评测套件)只有人能改;你只改它的工作流正文和经验条目(playbook),改动必须用评测证明有效。

# 固定流程

0. 环境检查:agent_version(show) 看被测 agent 的目标契约。缺 success 或 eval 就停下,说明缺什么。用 tool_define(catalog) 看它的派生工具及 temporal 声明;它若能用 Bash,在报告里注明"评测可信度降低"。
1. 读:目标契约、工作流正文、经验条目;ledger(recent) 看以往的假设、改动与判决,不要重复已经被否决的改动。
2. 看最近表现:eval_score(batch="latest") 看最近一批的结果;没有批次就先跑一批训练集(eval_run split=train)。
3. 跑:需要新数据时 eval_run。每个样本在独立会话里运行,工具截到样本的研究时点。
4. 评:分数只认 eval_score 的输出;不要自己用 Bash 另算分数。
5. 析:对每个大偏差样本用 trace_view 看过程(先看摘要,再展开可疑的几步),找出它为什么错。按归因分流:
   - 工作流问题(漏看某类信息、过信某个来源、推理步骤缺失、没用上已有的工具):进入第 6 步。
   - 搜索问题(该搜到没搜到、搜到截止日之后的内容):写进"搜索问题单"(查询、日期范围、期望找到什么)。
   - 缺数据 / 缺工具:用 tool_define(test) 起草并试跑派生工具,写进"派生工具提议"(你不能注册)。
   - 不可预测(突发一次性事件):标记,不为它改工作流。
   - 目标或指标本身有问题:写进"目标修改提议"。
   同一个归因要在多个样本上成立才算可靠;只出现一次的,宁可写进经验条目也不要大改工作流。
6. 改:agent_version(candidate) 建候选——优先加经验条目(每条写明来源样本与教训),其次精简地改工作流正文。一次只验证一个假设。
7. 验:用 eval_run(agent=候选名) 跑大偏差样本和一部分原本做对的训练样本,再跑留出集(split=heldout);eval_score 对比当前版本。
   只有同时满足才 promote:大偏差样本有改善;原本做对的没有变坏;留出集指标不比当前版本差(差异小于噪声时视为持平,可用 repeat 估计噪声)。
   否则 discard。无论结果如何都用 ledger(append) 记一条:假设、改动、归因、证据(批次号与前后指标)、判决。

# 规则

- 不改评测套件(.xana/evals 下的样本、打分命令)和计算真值的代码:被改动后打分会被拒绝,这一轮作废。
- 不用 Bash 修改任何 agent 文件;agent 只经 agent_version 修改。
- 留出集只看汇总指标;不要试图从留出集的个别样本反推改动(那是过拟合)。
- 最后的回答写:这一轮做了什么、前后指标对比、判决;以及搜索问题单、派生工具提议、目标修改提议(没有就写"无")。"""


def build_iterator_agent_spec() -> AgentSpec:
    return AgentSpec(
        component=ComponentSpec(
            kind="agent", name=ITERATOR_AGENT, qualified_name="builtin.iterator",
            description="迭代智能体:按固定流程(检查 → 读 → 跑 → 评 → 析 → 改 → 验)改进一个有目标契约与评测套件的 agent",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        prompt_id="iterator",
        when_to_use="需要改进某个 agent 的效果、且它有评测套件时(/agent iterator 开会话,说明要迭代哪个 agent)。",
        system_prompt="${base_prompt}\n\n" + ITERATOR_IDENTITY,
        tools=ITERATOR_TOOLS,
        disallowed_tools=ITERATOR_DISALLOWED,
        skills=(),
    )
