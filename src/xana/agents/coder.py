"""内置编码 agent:通用核心提示词 + 写代码的规则(docs/design/prompt-structure-design.md §2.2)。

规则的权威文本在 coding 技能(skills/bundled/coding.py):chat 写代码前按需 skill_load 加载;
整段编码工作用 `/agent coder` 切换,或 agent_run 委派给 coder(规则常驻系统提示词)。
"""
from __future__ import annotations

from xana.agents.contracts import AgentSpec
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.bundled.coding import CODING

CODER_AGENT = "coder"



def build_coder_agent_spec() -> AgentSpec:
    return AgentSpec(
        component=ComponentSpec(
            kind="agent", name=CODER_AGENT, qualified_name="builtin.coder",
            description="编码助手",
            source=ComponentSource(source_type="builtin", scope="system"),
        ),
        when_to_use="写代码、改代码、调试、跑测试、git 操作等软件工程任务",
        prompt_id="coder",
        system_prompt="${base_prompt}\n\n" + CODING,
    )
