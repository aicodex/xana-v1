"""SkillsListingContributor — 每轮注入「# 可用技能」段(名字+描述,不含全文)。

模板 AGENT_LOOP_PROMPT.md:136-140。全文经 skill_load 工具按需取。
构造注入 bundled skills registry(独立实例,不是 harness 的 skill_registry)。
"""
from __future__ import annotations

from xana.extension_api.contracts import ContextFragment, ExtensionData, TurnInputContext
from xana.extension_api.interfaces import TurnInputContributor
from xana.skills.contracts import SkillActivation, SkillSpec
from xana.skills.interfaces import SkillRegistry


SKILL_LOAD_TOOL = "skill_load"


class SkillsListingContributor(TurnInputContributor):
    """「# 可用技能」段(模板 AGENT_LOOP_PROMPT.md:136-140)。"""

    def __init__(self, *, skills: SkillRegistry) -> None:
        self._skills = skills

    async def contribute_turn_input(
        self,
        ctx: TurnInputContext,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
    ) -> list[ContextFragment]:
        if ctx.available_tools is not None and SKILL_LOAD_TOOL not in ctx.available_tools:
            return []  # 没有 skill_load 的 agent 用不了技能,不列
        specs = await self._list_specs(ctx)
        if not specs:
            return []
        lines = "\n".join(f"- {s.component.name}: {s.component.description}" for s in specs)
        return [ContextFragment(content=(
            "# 可用技能\n\n"
            "以下技能已加载到此会话。使用 skill_load 获取完整说明。当技能匹配任务时使用它。\n\n"
            + lines), source="skills_listing")]

    async def _list_specs(self, ctx: TurnInputContext) -> list[SkillSpec]:
        activation = SkillActivation(
            kind="system", session_id=ctx.session_id,
            agent_name=ctx.agent_name, run_id=ctx.run_id)
        return [s.spec() for s in await self._skills.discover(activation)]
