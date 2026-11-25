"""Skill abstract interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from xana.protocol.primitives import ComponentVersion
from xana.skills.contracts import (
    CatalogContext,
    CatalogPlan,
    ProposalStatus,
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillProposal,
    SkillSpec,
)


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


class SkillCatalogPolicy(ABC):
    """Evolvable skill catalog rendering & token-budget policy."""

    @abstractmethod
    def component_version(self) -> ComponentVersion: ...

    @abstractmethod
    def plan(self, specs: Sequence[SkillSpec], ctx: CatalogContext) -> CatalogPlan: ...


class SkillProposalQueue(ABC):
    """Lifecycle queue for candidate skill proposals (never auto-activates skills)."""

    @abstractmethod
    def submit(self, proposal: SkillProposal) -> SkillProposal: ...

    @abstractmethod
    def list(self, status: ProposalStatus | None = None) -> tuple[SkillProposal, ...]: ...

    @abstractmethod
    def get(self, proposal_id: str) -> SkillProposal | None: ...

    @abstractmethod
    def decide(
        self,
        proposal_id: str,
        *,
        status: Literal["approved", "rejected"],
        reason: str = "",
        now_ms: int | None = None,
    ) -> SkillProposal: ...

    @abstractmethod
    def prune_expired(self, now_ms: int | None = None) -> tuple[SkillProposal, ...]: ...
