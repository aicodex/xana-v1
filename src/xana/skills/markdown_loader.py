"""Markdown 文档加载器 —— SKILL.md 与 AGENT.md 共用的解析/加载实现(计划 §5.1)。

同一个解析器(SkillManifestParser 首个实现)按 frontmatter 的 `kind` 字段区分
skill(默认)与 agent;物理目录分开(~/.xana/skills/ 与 ~/.xana/agents/)。
校验失败的文档抛类型化错误 SkillManifestError(path/kind/field/reason),
目录扫描把错误收集成列表返回,不中断其余文档的加载。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import (
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillPackage,
    SkillSpec,
)
from xana.skills.interfaces import Skill, SkillLoader, SkillManifestParser

DOC_KINDS = ("skill", "agent")
_SKILL_DOC_NAME = "SKILL.md"


class SkillManifestError(ValueError):
    """文档 frontmatter 校验失败的类型化错误。"""

    def __init__(self, *, path: Path, kind: str, field: str, reason: str) -> None:
        self.path = Path(path)
        self.kind = kind
        self.field = field
        self.reason = reason
        super().__init__(f"{self.path}: [{kind}] {field}: {reason}")


def split_frontmatter(text: str, *, path: Path):
    """切出 (frontmatter dict, 正文)。无 frontmatter 时返回 ({}, text)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillManifestError(
            path=path, kind="?", field="frontmatter",
            reason="缺少结束的 --- 行",
        ) from None
    raw = "\n".join(lines[1:end])
    data = yaml.safe_load(raw) if raw.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SkillManifestError(
            path=path, kind="?", field="frontmatter",
            reason="frontmatter 必须是 YAML mapping",
        )
    body = "\n".join(lines[end + 1:]).strip("\n")
    return data, body


def _require_str(fm: dict[str, Any], *, path: Path, kind: str, field: str) -> str:
    value = fm.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SkillManifestError(
            path=path, kind=kind, field=field,
            reason=f"{kind} 文档必须提供非空 {field}",
        )
    return value.strip()


def _optional_str_list(fm: dict[str, Any], *, path: Path, kind: str, field: str) -> tuple[str, ...]:
    value = fm.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SkillManifestError(
            path=path, kind=kind, field=field,
            reason=f"{field} 必须是字符串列表",
        )
    return tuple(value)


class MarkdownDocParser(SkillManifestParser):
    """SkillManifestParser 首个实现:frontmatter yaml + 正文,双 kind。

    - kind=skill(默认):必填 name/description;可选 when_to_use/allowed_tools。
    - kind=agent:必填 name/goal;goal 映射到 manifest.description;可选 when_to_use / tools(不写 = 默认组合,
      [] = 零工具)/ disallowed_tools / skills/budget/stop_rules/output_contract/prompt_profile/verify
      放入 metadata,由 AgentRegistry 消费(docs/design/agent-composition-design.md §3.3)。
    """

    async def parse(self, path: Path) -> SkillManifest:
        path = Path(path)
        fm, _body = split_frontmatter(path.read_text(encoding="utf-8"), path=path)
        kind = fm.get("kind") or "skill"
        if kind not in DOC_KINDS:
            raise SkillManifestError(
                path=path, kind=str(kind), field="kind",
                reason=f"kind 必须是 {'|'.join(DOC_KINDS)}",
            )
        name = _require_str(fm, path=path, kind=kind, field="name")
        if kind == "agent":
            goal = _require_str(fm, path=path, kind=kind, field="goal")
            metadata: dict[str, Any] = {"kind": "agent"}
            when_to_use = fm.get("when_to_use")
            if when_to_use is not None:
                if not isinstance(when_to_use, str):
                    raise SkillManifestError(path=path, kind=kind, field="when_to_use",
                                             reason="when_to_use 必须是字符串")
                metadata["when_to_use"] = when_to_use
            for list_field in ("tools", "disallowed_tools"):
                if fm.get(list_field) is not None:  # tools 不写 = 默认组合;[] = 零工具
                    metadata[list_field] = list(
                        _optional_str_list(fm, path=path, kind=kind, field=list_field))
            if fm.get("skills") is not None:   # 不写 = 项目默认;[] = 没有技能
                metadata["skills"] = list(_optional_str_list(fm, path=path, kind=kind, field="skills"))
            budget = fm.get("budget")
            if budget is not None:
                if not isinstance(budget, dict):
                    raise SkillManifestError(
                        path=path, kind=kind, field="budget",
                        reason="budget 必须是 mapping",
                    )
                metadata["budget"] = dict(budget)
            stop_rules = _optional_str_list(fm, path=path, kind=kind, field="stop_rules")
            if stop_rules:
                metadata["stop_rules"] = list(stop_rules)
            output_contract = fm.get("output_contract")
            if output_contract is not None:
                if not isinstance(output_contract, str):
                    raise SkillManifestError(
                        path=path, kind=kind, field="output_contract",
                        reason="output_contract 必须是字符串",
                    )
                metadata["output_contract"] = output_contract
            verify = fm.get("verify")
            if verify is not None:
                if not isinstance(verify, list):
                    raise SkillManifestError(
                        path=path, kind=kind, field="verify",
                        reason="verify 必须是命令列表",
                    )
                metadata["verify"] = list(verify)
            prompt_profile = fm.get("prompt_profile")
            if prompt_profile is not None:
                if not isinstance(prompt_profile, (str, dict)):
                    raise SkillManifestError(
                        path=path, kind=kind, field="prompt_profile",
                        reason="prompt_profile 必须是模式名或 mapping",
                    )
                metadata["prompt_profile"] = (
                    dict(prompt_profile) if isinstance(prompt_profile, dict) else prompt_profile)
            return SkillManifest(
                name=name,
                description=goal,
                metadata=metadata,
            )
        description = _require_str(fm, path=path, kind=kind, field="description")
        when_to_use = fm.get("when_to_use")
        if when_to_use is not None and not isinstance(when_to_use, str):
            raise SkillManifestError(
                path=path, kind=kind, field="when_to_use",
                reason="when_to_use 必须是字符串",
            )
        return SkillManifest(
            name=name,
            description=description,
            when_to_use=(when_to_use or "").strip(),
            allowed_tools=_optional_str_list(fm, path=path, kind=kind, field="allowed_tools"),
            metadata={"kind": "skill"},
        )


class MarkdownSkill(Skill):
    """文件 SKILL.md 的 Skill 实现:正文经 activate 按需返回(同 bundled 路径)。"""

    def __init__(self, *, spec: SkillSpec, body: str) -> None:
        self._spec = spec
        self._body = body

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(prompt_blocks=(self._body,))

    async def deactivate(self, session_id: str) -> None:
        return None


class FileSkillLoader(SkillLoader):
    """SkillLoader 首个实现:source 是 SKILL.md 路径(或其所在目录)。

    与 bundled skill 产出同一种 Skill 接口对象,注册进同一个 registry,
    走同一个 skill_load 两阶段路径(计划 §5.2:不互相引用实现,只复用解析器)。
    """

    def __init__(self, *, parser: SkillManifestParser | None = None) -> None:
        self._parser = parser or MarkdownDocParser()

    async def load(self, source: str) -> Skill:
        path = Path(source).expanduser()
        if path.is_dir():
            path = path / _SKILL_DOC_NAME
        manifest = await self._parser.parse(path)
        if manifest.metadata.get("kind") != "skill":
            raise SkillManifestError(
                path=path, kind=str(manifest.metadata.get("kind")), field="kind",
                reason="AGENT.md 是角色定义文档,经 AgentRegistry 加载,不是 skill",
            )
        _fm, body = split_frontmatter(path.read_text(encoding="utf-8"), path=path)
        spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name=manifest.name,
                qualified_name=f"user.{manifest.name}",
                description=manifest.description,
                source=ComponentSource(source_type="user", scope="user", path=path),
            ),
            when_to_use=manifest.when_to_use,
            package=SkillPackage(
                root=path.parent,
                manifest=manifest,
                instructions_path=path,
            ),
            allowed_tools=manifest.allowed_tools,
        )
        return MarkdownSkill(spec=spec, body=body)


async def scan_skill_docs(root: Path, *, loader: SkillLoader | None = None):
    """扫描 {root}/*/SKILL.md,返回 (skills, errors)。坏文档不中断扫描。"""
    loader = loader or FileSkillLoader()
    root = Path(root)
    skills: list[Skill] = []
    errors: list[SkillManifestError] = []
    if not root.is_dir():
        return (), ()
    for path in sorted(root.glob(f"*/{_SKILL_DOC_NAME}")):
        try:
            skills.append(await loader.load(str(path)))
        except SkillManifestError as err:
            errors.append(err)
    return tuple(skills), tuple(errors)
