"""Skill abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from xana.skills.contracts import SkillActivation, SkillContext, SkillContribution, SkillManifest, SkillPackage, SkillResource, SkillSpec


class SkillManifestParser(ABC):
    @abstractmethod
    async def parse(self, path: Path) -> SkillManifest: ...


class SkillResourceResolver(ABC):
    @abstractmethod
    async def resolve(self, package: SkillPackage, resource: SkillResource) -> Path: ...


class SkillPromptProvider(ABC):
    @abstractmethod
    async def prompt(self, activation: SkillActivation) -> str: ...


class SkillContextLoader(ABC):
    @abstractmethod
    async def load(self, activation: SkillActivation) -> SkillContext: ...


class SkillActivator(ABC):
    @abstractmethod
    async def should_activate(self, activation: SkillActivation) -> bool: ...


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
