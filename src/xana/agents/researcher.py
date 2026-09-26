"""内置子研究员 agent:deep_research 派出的并行子 agent(docs/design/lead-research-design.md)。

只能用 web_search / web_open_url / read;不向用户提问;交回带出处的发现。白名单里没有
deep_research,子研究员不会再派子 agent(防递归)。
"""
from __future__ import annotations

from xana.agents.contracts import AgentSpec
from xana.component.contracts import ComponentSource, ComponentSpec

RESEARCHER_AGENT = "researcher"
RESEARCHER_TOOLS = ("web_search", "web_open_url", "read")
RESEARCHER_MAX_TURNS = 20

RESEARCHER_IDENTITY = (
    "你是研究团队里的一名子研究员,负责一个具体的子课题,由研究负责人分派。\n"
    "用 web_search 找线索(返回的是搜索摘要,不是全文);对要引用的事实和数字,用 web_open_url 打开原文核实。"
    "优先公司公告、财报、交易所与监管披露、权威媒体;不同来源说法矛盾时都记下来。\n"
    "不要向用户提问,也不要等待确认:信息不足时如实写明。完成后直接输出你的发现:每条事实后附来源 "
    "[标题](url),写清哪些没能确认。"
)


def build_researcher_agent_spec() -> AgentSpec:
    return AgentSpec(
        component=ComponentSpec(
            kind="agent", name=RESEARCHER_AGENT, qualified_name="builtin.researcher",
            description="deep_research 的子研究员:检索、阅读原文、交回带出处的发现",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        prompt_id="researcher",
        identity=RESEARCHER_IDENTITY,
        allowed_tools=RESEARCHER_TOOLS,
        max_turns=RESEARCHER_MAX_TURNS,
    )
