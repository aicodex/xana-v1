"""AgentListingContributor — 本 agent 可用 agent_run 时,每条用户消息注入可委派 agent 列表。

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
            "用 agent_run 委派任务时可选的 subagent_type:\n\n"
            + agent_listing(self._agents, ceiling, specs, toolsets)), source="agent_listing")]
