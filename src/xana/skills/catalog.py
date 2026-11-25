"""Default SkillCatalogPolicy: deterministic catalog rendering, token budgeting, and prefix stability."""
from __future__ import annotations

from collections.abc import Sequence

from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion
from xana.protocol.versioning import canonical_json, sha256_text
from xana.skills.contracts import CatalogContext, CatalogEntry, CatalogPlan, SkillSpec
from xana.skills.interfaces import SkillCatalogPolicy

SKILL_LOAD_TOOL = "skill_load"
_CATALOG_HEADER = (
    "# 可用技能\n\n"
    "以下技能已加载到此会话。使用 skill_load 获取完整说明。当技能匹配任务时使用它。\n\n"
)
_DIAG = DiagLogger("skills")


class DefaultSkillCatalogPolicy(SkillCatalogPolicy):
    """Renders `# 可用技能` catalog with optional entry/char caps and stable hash."""

    POLICY_VERSION = "1.0.0"

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="default_skill_catalog_policy",
            kind="skills_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
        )

    def plan(self, specs: Sequence[SkillSpec], ctx: CatalogContext) -> CatalogPlan:
        if ctx.available_tools is not None and SKILL_LOAD_TOOL not in ctx.available_tools:
            return CatalogPlan()

        filtered = [
            s
            for s in specs
            if not s.disable_model_invocation
            and (ctx.visible_skills is None or s.component.name in ctx.visible_skills)
        ]
        if not filtered:
            return CatalogPlan()

        entries: list[CatalogEntry] = []
        lines: list[str] = []
        total_chars = len(_CATALOG_HEADER)
        omitted = 0

        for s in filtered:
            line = f"- {s.component.name}: {s.component.description}"
            if ctx.max_entries is not None and len(entries) >= ctx.max_entries:
                omitted += 1
                continue
            if ctx.max_chars is not None and (total_chars + len(line) + 1) > ctx.max_chars:
                omitted += 1
                continue
            entries.append(
                CatalogEntry(
                    name=s.component.name,
                    description=s.component.description,
                    when_to_use=s.when_to_use,
                    trust_level=s.trust_level,
                    source_type=s.component.source.source_type,
                    char_count=len(line),
                )
            )
            lines.append(line)
            total_chars += len(line) + 1

        if not entries:
            return CatalogPlan(omitted_count=omitted)

        rendered = _CATALOG_HEADER + "\n".join(lines)
        cat_hash = sha256_text(canonical_json([e.name for e in entries]) + rendered)[:16]
        plan = CatalogPlan(
            entries=tuple(entries),
            rendered=rendered,
            total_chars=len(rendered),
            estimated_tokens=max(1, len(rendered) // 4),
            omitted_count=omitted,
            catalog_hash=cat_hash,
        )
        if omitted > 0:
            _DIAG.anomaly("catalog.capped", agent=ctx.agent_name, included=len(entries), omitted=omitted, chars=len(rendered))
        else:
            _DIAG.detail("catalog.planned", agent=ctx.agent_name, entries=len(entries), chars=len(rendered), hash=cat_hash)
        return plan
