"""In-memory SkillRegistry.

discover() returns all registered skills for v1. Future versions can filter
by activation_paths against cwd / kind / agent.
"""
from __future__ import annotations

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
