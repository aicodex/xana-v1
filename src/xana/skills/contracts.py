"""Skill contracts.

Skills are packages of instructions, resources, and optional scripts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from xana.component.contracts import ComponentSpec

SkillMode = Literal["inline", "fork"]
ActivationKind = Literal["manual", "path", "model", "system"]
TrustLevel = Literal["trusted", "local", "remote_untrusted"]
ResourceKind = Literal["instructions", "reference", "template", "asset", "script"]


@dataclass(frozen=True)
class SkillResource:
    kind: ResourceKind
    path: Path
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillScript:
    name: str
    path: Path
    args_schema: dict[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    version: str | None = None
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = ()
    activation_paths: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillPackage:
    root: Path | None
    manifest: SkillManifest
    instructions_path: Path | None
    resources: tuple[SkillResource, ...] = ()
    scripts: tuple[SkillScript, ...] = ()


@dataclass(frozen=True)
class SkillSpec:
    component: ComponentSpec
    when_to_use: str
    package: SkillPackage
    mode: SkillMode = "inline"
    allowed_tools: tuple[str, ...] = ()
    activation_paths: tuple[str, ...] = ()
    trust_level: TrustLevel = "local"
    dependencies: tuple[str, ...] = ()
    arg_schema: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    effort: str | int | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class SkillActivation:
    kind: ActivationKind
    session_id: str
    agent_name: str
    run_id: str | None = None
    cwd: Path | None = None
    args: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillContext:
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillContribution:
    prompt_blocks: tuple[str, ...] = ()
    context: SkillContext = field(default_factory=SkillContext)
    metadata: dict[str, Any] = field(default_factory=dict)
