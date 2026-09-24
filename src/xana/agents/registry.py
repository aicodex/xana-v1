"""FileAgentRegistry —— AgentRegistry 首个实现(计划 §5.1)。

从 {root}/*/AGENT.md 加载角色定义文档,生成 AgentSpec:
- 正文(工作流步骤)+ goal 渲染进 spec.identity(插在 INTRO 与 SYSTEM 之间,
  保留默认 13 段系统提示 —— 不用 system_prompt override)
- tools 白名单 → allowed_tools;skills 列表 → allowed_skills
- budget/stop_rules/output_contract 放入 component.metadata(阶段四消费)

Agent 实例经注入的 agent_factory 构造(boot 用共享 harness/stores 的
ChatAgent 工厂),加载后由 boot 注册进 gateway.agents。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from xana.harness_sdk.contracts import PromptProfile
from xana.agents.contracts import AgentSpec
from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import SkillManifest
from xana.skills.interfaces import SkillManifestParser
from xana.skills.markdown_loader import SkillManifestError, split_frontmatter

_AGENT_DOC_NAME = "AGENT.md"


def agent_spec_from_doc(manifest: SkillManifest, *, body: str, path: Path) -> AgentSpec:
    """AGENT.md 的 manifest + 正文 → AgentSpec 字段映射。"""
    if manifest.metadata.get("kind") != "agent":
        raise SkillManifestError(
            path=path, kind=str(manifest.metadata.get("kind")), field="kind",
            reason="agent_spec_from_doc 只接受 kind=agent 的文档",
        )
    md = dict(manifest.metadata)
    component_metadata: dict = {}
    for key in ("budget", "stop_rules", "output_contract"):
        if key in md:
            component_metadata[key] = md[key]
    identity = f"# {manifest.name}\n\n目标:{manifest.description}\n\n{body}".strip()
    prompt_profile = None
    if "prompt_profile" in md:
        raw = md["prompt_profile"]
        try:
            prompt_profile = PromptProfile.from_dict(raw if isinstance(raw, dict) else {"mode": raw})
        except (TypeError, ValueError) as exc:
            raise SkillManifestError(path=path, kind="agent", field="prompt_profile",
                                     reason=str(exc)) from exc
    return AgentSpec(
        component=ComponentSpec(
            kind="agent",
            name=manifest.name,
            qualified_name=f"user.{manifest.name}",
            description=manifest.description,
            source=ComponentSource(source_type="user", scope="user", path=Path(path)),
            metadata=component_metadata,
        ),
        prompt_id=f"agent.{manifest.name}",
        allowed_tools=manifest.allowed_tools,
        allowed_skills=tuple(md.get("skills", ())),
        identity=identity,
        prompt_profile=prompt_profile,
    )


class FileAgentRegistry(AgentRegistry):
    """从文件系统目录加载 AGENT.md 文档的 AgentRegistry。"""

    def __init__(
        self,
        *,
        root: Path,
        parser: SkillManifestParser,
        agent_factory: Callable[[AgentSpec], Agent],
    ) -> None:
        self._root = Path(root)
        self._parser = parser
        self._agent_factory = agent_factory
        self._agents: dict[str, Agent] = {}

    async def load(self) -> tuple[SkillManifestError, ...]:
        """扫描 {root}/*/AGENT.md。坏文档收集为错误返回,不中断扫描。"""
        errors: list[SkillManifestError] = []
        if not self._root.is_dir():
            return ()
        for path in sorted(self._root.glob(f"*/{_AGENT_DOC_NAME}")):
            try:
                manifest = await self._parser.parse(path)
                _fm, body = split_frontmatter(
                    path.read_text(encoding="utf-8"), path=path,
                )
                await self.register(self._agent_factory(
                    agent_spec_from_doc(manifest, body=body, path=path),
                ))
            except SkillManifestError as err:
                errors.append(err)
        return tuple(errors)

    async def register(self, agent: Agent) -> None:
        self._agents[agent.spec().component.name] = agent

    async def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    async def list_specs(self) -> tuple[AgentSpec, ...]:
        return tuple(agent.spec() for agent in self._agents.values())
