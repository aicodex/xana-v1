"""Skill abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from xana.skills.contracts import SkillActivation, SkillContribution, SkillManifest, SkillSpec


class SkillManifestParser(ABC):
    @abstractmethod
    async def parse(self, path: Path) -> SkillManifest: ...


class Skill(ABC):
    @abstractmethod
    def spec(self) -> SkillSpec: ...

    @abstractmethod
    async def activate(self, activation: SkillActivation) -> SkillContribution: ...

    @abstractmethod
    async def deactivate(self, session_id: str) -> None: ...


class SkillLoader(ABC):
    @abstractmethod
    async def load(self, source: str) -> Skill: ...


class SkillRegistry(ABC):
    @abstractmethod
    async def register(self, skill: Skill) -> None: ...

    @abstractmethod
    async def get(self, name: str) -> Skill | None: ...

    @abstractmethod
    async def discover(self, activation: SkillActivation) -> tuple[Skill, ...]: ...
