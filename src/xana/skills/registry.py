"""Skill registries: builtins and explicitly activated external packages."""
from __future__ import annotations

from xana.skills.management import SkillManagement
from xana.skills.contracts import SkillActivation
from xana.skills.interfaces import Skill, SkillRegistry


class InMemorySkillRegistry(SkillRegistry):
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    async def register(self, skill: Skill) -> None:
        name = skill.spec().component.name
        self._skills[name] = skill

    async def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    async def discover(self, activation: SkillActivation) -> tuple[Skill, ...]:
        return tuple(self._skills.values())


class ManagedSkillRegistry(InMemorySkillRegistry):
    """Existing registered skills plus packages explicitly enabled for this agent."""

    def __init__(self, *, management: SkillManagement) -> None:
        super().__init__()
        self._management = management

    async def discover(self, activation: SkillActivation) -> tuple[Skill, ...]:
        base = {s.spec().component.name: s for s in await super().discover(activation)}
        base.update({s.spec().component.name: s for s in await self._management.enabled(activation.agent_name)})
        return tuple(base.values())
