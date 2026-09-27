"""AgentListingContributor — 本 agent 可用 agent_run 时,每条用户消息注入委派引导与可委派 agent 列表。

列表以 note 追加进历史(内容不变不重复注入),不写进 agent_run 的工具描述,保持工具 schema 稳定
(docs/design/agent-delegation-design.md §3.3)。每项工具按子 agent 实际会拿到的有效工具展开。
"""
from __future__ import annotations

from collections.abc import Mapping

from xana.agents.delegation import AGENT_RUN_TOOL, agent_listing, subagent_ceiling
from xana.agents.interfaces import Agent, AgentRegistry
from xana.extension_api.contracts import ContextFragment, ExtensionData, TurnInputContext
from xana.extension_api.interfaces import TurnInputContributor
from xana.tools.interfaces import ToolRegistry


# 委派引导(译自 Claude Code 2.1.281 系统提示词的 Agent 工具段,并按实验补充"先委派、别先自己读一遍"):
# 实验(docs/design/agent-delegation-design.md §6)里没有这段时,模型先自己读了一大圈再委派,用时约 2.5 倍。
DELEGATION_GUIDE = (
    "# 委派子 agent\n\n"
    "当任务与某个 agent 的说明相符时,用 agent_run 委派给它。子 agent 适合两类情况:并行处理互不依赖的子任务;"
    "把大量原始内容(超大文件、大批网页、大范围搜索结果)挡在你的上下文之外,只取回结论。"
    "决定委派就直接委派,不要先自己把材料读一遍;已经委派出去的调研,你自己不要再重复做。"
    "不需要时不要滥用:几步就能完成的事自己做。"
)


class AgentListingContributor(TurnInputContributor):
    def __init__(self, *, agents: Mapping[str, Agent], agent_registry: AgentRegistry | None,
                 tool_registry: ToolRegistry) -> None:
        self._agents = agents            # 活的映射:增删改的 agent 下一条消息起反映在列表里
        self._agent_registry = agent_registry
        self._tools = tool_registry

    async def contribute_turn_input(
        self,
        ctx: TurnInputContext,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
    ) -> list[ContextFragment]:
        if ctx.available_tools is not None and AGENT_RUN_TOOL not in ctx.available_tools:
            return []
        if self._agent_registry is not None:
            await self._agent_registry.refresh()
        if not self._agents:
            return []
        specs = await self._tools.list_specs()
        toolsets = await self._tools.list_toolsets()
        ceiling = subagent_ceiling(ctx.available_tools, specs, toolsets)
        return [ContextFragment(content=(
            DELEGATION_GUIDE + "\n\n用 agent_run 委派任务时可选的 subagent_type:\n\n"
            + agent_listing(self._agents, ceiling, specs, toolsets)), source="agent_listing")]
