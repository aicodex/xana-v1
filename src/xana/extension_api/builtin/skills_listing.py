"""SkillsListingContributor — 每轮注入「# 可用技能」段(名字+描述,不含全文)。

模板 AGENT_LOOP_PROMPT.md:136-140。全文经 skill_load 工具按需取。
构造注入 bundled skills registry(独立实例,不是 harness 的 skill_registry)。
"""
from __future__ import annotations

from xana.extension_api.contracts import ContextFragment, ExtensionData, TurnInputContext
from xana.extension_api.interfaces import TurnInputContributor
from xana.skills.catalog import DefaultSkillCatalogPolicy
from xana.skills.contracts import CatalogContext, SkillActivation, SkillSpec
from xana.skills.interfaces import SkillCatalogPolicy, SkillRegistry
from xana.skills.tape_recorder import SkillTapeRecorder


SKILL_LOAD_TOOL = "skill_load"


class SkillsListingContributor(TurnInputContributor):
    """「# 可用技能」段(模板 AGENT_LOOP_PROMPT.md:136-140)。"""

    def __init__(
        self,
        *,
        skills: SkillRegistry,
        catalog_policy: SkillCatalogPolicy | None = None,
        recorder: SkillTapeRecorder | None = None,
    ) -> None:
        self._skills = skills
        self._catalog_policy: SkillCatalogPolicy = catalog_policy or DefaultSkillCatalogPolicy()
        self._recorder = recorder or SkillTapeRecorder(None)

    async def contribute_turn_input(
        self,
        ctx: TurnInputContext,
        thread_store: ExtensionData,
        turn_store: ExtensionData,
    ) -> list[ContextFragment]:
        specs = await self._list_specs(ctx)
        cat_ctx = CatalogContext(
            session_id=ctx.session_id,
            agent_name=ctx.agent_name,
            run_id=ctx.run_id or "",
            available_tools=tuple(sorted(ctx.available_tools)) if ctx.available_tools is not None else None,
            visible_skills=tuple(sorted(ctx.visible_skills)) if ctx.visible_skills is not None else None,
        )
        plan = self._catalog_policy.plan(specs, cat_ctx)
        if not plan.rendered:
            return []
        self._recorder.record_catalog(cat_ctx, plan, policy=self._catalog_policy.component_version())
        return [ContextFragment(content=plan.rendered, source="skills_listing")]

    async def _list_specs(self, ctx: TurnInputContext) -> list[SkillSpec]:
        activation = SkillActivation(
            kind="system", session_id=ctx.session_id,
            agent_name=ctx.agent_name, run_id=ctx.run_id)
        return [s.spec() for s in await self._skills.discover(activation)]
