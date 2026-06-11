"""identity: minimal always-on skill that injects an identity prompt block."""
from __future__ import annotations

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import (
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillPackage,
    SkillSpec,
)
from xana.skills.interfaces import Skill


_DEFAULT_IDENTITY = (
    "You are xana, a clean refactor of the yuxu agent runtime. "
    "Answer concisely and use tools when they help."
)


class IdentitySkill(Skill):
    def __init__(self, identity_text: str = _DEFAULT_IDENTITY) -> None:
        self.identity_text = identity_text
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="identity",
                qualified_name="builtin.identity",
                description="Always-on identity prompt for the chat agent.",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="every turn",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="identity", description="agent identity"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(self.identity_text,))

    async def deactivate(self, session_id: str) -> None:
        return None
