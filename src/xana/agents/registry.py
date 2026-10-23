"""FileAgentRegistry —— AgentRegistry 首个实现:从两级目录加载 AGENT.md,热加载
(docs/design/agent-delegation-design.md §7)。

- 目录按优先级从低到高:用户级 {XANA_HOME}/agents、项目级 <project_root>/.xana/agents;同名时高优先级覆盖。
- 直接维护 gateway 共用的 agent 表(table):refresh() 比对每个 AGENT.md 的 (路径, 修改时间, 大小),
  新增 / 变更的重新加载,文件已不存在的移出表;内置 agent 名(reserved)不能被文件覆盖。
- 文档 → AgentSpec 的字段映射见 agent_spec_from_doc:正文即系统提示词模板;tools 工具选择表达式
  (不写 = 项目默认,[] = 零工具)/ disallowed_tools;skills 技能选择(不写 = 项目默认);
  budget/stop_rules/output_contract 放入 component.metadata。
Agent 实例经注入的 agent_factory 构造(boot 用共享 harness/stores 的 ChatAgent 工厂)。
"""
from __future__ import annotations

import asyncio
import logging
import time

import yaml
from collections.abc import Callable, Collection, MutableMapping, Sequence
from pathlib import Path

from xana.harness_sdk.contracts import PromptProfile, VerifyCheck
from xana.agents.contracts import EXEC_AGENTS, AgentSpec, GoalContract
from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentScope, ComponentSource, ComponentSpec
from xana.config.system_model import AgentDefaults
from xana.skills.contracts import SkillManifest
from xana.skills.interfaces import SkillManifestParser
from xana.skills.markdown_loader import SkillManifestError, split_frontmatter

_logger = logging.getLogger(__name__)
_AGENT_DOC_NAME = "AGENT.md"


def agent_spec_from_doc(manifest: SkillManifest, *, body: str, path: Path,
                        scope: ComponentScope = "user", defaults: AgentDefaults | None = None) -> AgentSpec:
    """AGENT.md 的 manifest + 正文 → AgentSpec 字段映射。没写 tools / skills 时用项目默认(defaults);
    没写 tools 时项目默认的 disallowed_tools 也并入。"""
    defaults = defaults or AgentDefaults()
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
    try:
        verify = tuple(VerifyCheck.from_raw(item) for item in md.get("verify", ()))
    except (TypeError, ValueError) as exc:
        raise SkillManifestError(path=path, kind="agent", field="verify", reason=str(exc)) from exc
    prompt_profile = None
    if "prompt_profile" in md:
        raw = md["prompt_profile"]
        try:
            prompt_profile = PromptProfile.from_dict(raw if isinstance(raw, dict) else {"mode": raw})
        except (TypeError, ValueError) as exc:
            raise SkillManifestError(path=path, kind="agent", field="prompt_profile",
                                     reason=str(exc)) from exc
    contract = GoalContract(
        goal=manifest.description,
        success=dict(md.get("success") or {}),
        constraints=tuple(md.get("constraints") or ()),
        non_goals=tuple(md.get("non_goals") or ()),
        eval=md.get("eval"),
        allow_exec=bool(md.get("allow_exec")),
        allow_exec_reason=str(md.get("allow_exec_reason") or ""),
    )
    tools = tuple(md["tools"]) if "tools" in md else tuple(defaults.tools)
    disallowed = tuple(md.get("disallowed_tools", ())) + (() if "tools" in md else tuple(defaults.disallowed_tools))
    # Bash 规则(§5.4):文件 agent 默认没有 exec 组,需要执行能力时派生工具;用户开启 allow_exec 才保留
    if manifest.name not in EXEC_AGENTS and not contract.allow_exec:
        if any(t in ("exec", "Bash") for t in tools):
            _logger.warning("agent %s 选了 exec / Bash 但没有 allow_exec,已去掉;需要执行能力请派生工具"
                            "(tool_creator),或由用户 /agent allow-exec %s <原因>", manifest.name, manifest.name)
        if "exec" not in disallowed:
            disallowed += ("exec",)
    return AgentSpec(
        component=ComponentSpec(
            kind="agent",
            name=manifest.name,
            qualified_name=f"user.{manifest.name}",
            description=manifest.description,
            source=ComponentSource(source_type=scope, scope=scope, path=Path(path)),
            metadata=component_metadata,
        ),
        prompt_id=f"agent.{manifest.name}",
        tools=tools,
        disallowed_tools=disallowed,
        contract=contract,
        skills=tuple(md["skills"]) if "skills" in md else (
            tuple(defaults.skills) if defaults.skills is not None else None),
        when_to_use=str(md.get("when_to_use") or ""),
        system_prompt=body.strip() or None,
        prompt_profile=prompt_profile,
        verify=verify,
        model=md.get("model"),
        effort=md.get("effort"),
        max_turns=md.get("max_turns"),
    )


PLAYBOOK_NAME = "playbook.md"
PLAYBOOK_HEADING = "# 经验条目(迭代中积累,每条注明来源)"


def _playbook_version(agent_doc: Path) -> tuple[int, int]:
    """同目录 playbook.md 的版本:它变了 agent 也要重新加载(经验条目拼在系统提示词末尾)。"""
    try:
        st = (agent_doc.parent / PLAYBOOK_NAME).stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


class FileAgentRegistry(AgentRegistry):
    """两级目录的 AGENT.md → gateway 共用的 agent 表,支持热加载与删除。"""

    def __init__(
        self,
        *,
        roots: Sequence[tuple[ComponentScope, Path]],
        parser: SkillManifestParser,
        agent_factory: Callable[[AgentSpec], Agent],
        table: MutableMapping[str, Agent] | None = None,
        reserved: Collection[str] = (),
        min_interval_s: float = 2.0,
        defaults: AgentDefaults | None = None,
    ) -> None:
        """roots 按优先级从低到高;table 是调用方共用的 agent 表(缺省自建);reserved 是内置 agent 名。"""
        self._roots = tuple((scope, Path(root)) for scope, root in roots)
        self._parser = parser
        self._agent_factory = agent_factory
        self._table: MutableMapping[str, Agent] = table if table is not None else {}
        self._reserved = frozenset(reserved)
        self._min_interval_s = min_interval_s
        self._defaults = defaults            # 项目给 agent 的默认配置(没写 tools / skills 的 AGENT.md 用)
        # 文件版本 = (修改时间 ns, 大小):同一时间刻度内的连续修改靠大小区分
        self._parsed: dict[Path, tuple[tuple, AgentSpec]] = {}   # 每个文件最近一次成功解析的版本
        self._loaded: dict[str, tuple[Path, tuple]] = {}         # 表中由本注册表维护的 agent → 来源版本
        self._reported: set[tuple[Path, tuple]] = set()          # 已告警过的 (路径, 版本)
        self._last_scan = float("-inf")
        self._lock = asyncio.Lock()

    async def load(self) -> tuple[SkillManifestError, ...]:
        """立即扫描(boot 调用);坏文档收集为错误返回,不中断扫描。"""
        return await self.refresh(force=True)

    async def refresh(self, *, force: bool = False) -> tuple[SkillManifestError, ...]:
        """按需同步 agent 表与磁盘;距上次扫描不足 min_interval_s 时直接返回(force 除外)。
        返回本次新出现的解析错误(同一版本的坏文件只报一次)。"""
        if not force and time.monotonic() - self._last_scan < self._min_interval_s:
            return ()
        async with self._lock:
            self._last_scan = time.monotonic()
            return await self._sync()

    async def _sync(self) -> tuple[SkillManifestError, ...]:
        errors: list[SkillManifestError] = []
        chosen: dict[str, tuple[Path, tuple, AgentSpec]] = {}
        seen: set[Path] = set()
        for scope, root in self._roots:                     # 低 → 高,后写覆盖
            if not root.is_dir():
                continue
            for path in sorted(root.glob(f"*/{_AGENT_DOC_NAME}")):
                try:
                    st = path.stat()
                except OSError:
                    continue
                mtime = (st.st_mtime_ns, st.st_size, _playbook_version(path))
                seen.add(path)
                cached = self._parsed.get(path)
                if cached is None or cached[0] != mtime:
                    try:
                        self._parsed[path] = (mtime, await self._parse(path, scope))
                    except SkillManifestError as err:
                        if (path, mtime) not in self._reported:
                            self._reported.add((path, mtime))
                            errors.append(err)
                        if cached is None:
                            continue                        # 从未成功加载过:跳过
                        # 改坏了:保留上一个可用版本,不静默删掉正在用的 agent
                cur_mtime, spec = self._parsed[path]
                name = spec.component.name
                if name in self._reserved:
                    if (path, cur_mtime) not in self._reported:
                        self._reported.add((path, cur_mtime))
                        errors.append(SkillManifestError(path=path, kind="agent", field="name",
                                                         reason=f"{name} 是内置 agent,不能被文件覆盖"))
                    continue
                chosen[name] = (path, cur_mtime, spec)
        for path in [p for p in self._parsed if p not in seen]:
            del self._parsed[path]
        for name, (path, mtime, spec) in chosen.items():
            if self._loaded.get(name) != (path, mtime):
                self._table[name] = self._agent_factory(spec)
                self._loaded[name] = (path, mtime)
        for name in [n for n in self._loaded if n not in chosen]:
            self._table.pop(name, None)
            del self._loaded[name]
        for err in errors:
            _logger.warning("AGENT.md 加载失败: %s", err)
        return tuple(errors)

    async def _parse(self, path: Path, scope: ComponentScope) -> AgentSpec:
        manifest = await self._parser.parse(path)
        _fm, body = split_frontmatter(path.read_text(encoding="utf-8"), path=path)
        playbook = path.parent / PLAYBOOK_NAME
        if playbook.exists() and playbook.read_text(encoding="utf-8").strip():
            body = body.rstrip() + "\n\n" + PLAYBOOK_HEADING + "\n\n" + playbook.read_text(encoding="utf-8").strip()
        return agent_spec_from_doc(manifest, body=body, path=path, scope=scope, defaults=self._defaults)

    async def remove(self, name: str) -> Path:
        """删除文件 agent(删掉它当前生效的 AGENT.md)并立即刷新;内置或不存在的 agent 抛 LookupError。
        删掉项目级的同名 agent 后,用户级同名 agent(若有)随即生效。"""
        await self.refresh(force=True)
        if name not in self._loaded:
            raise LookupError(f"没有名为 {name!r} 的文件 agent(内置 agent 不能删除)")
        path, _mtime = self._loaded[name]
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()                             # 目录空了才删
        except OSError:
            pass
        await self.refresh(force=True)
        return path

    async def set_allow_exec(self, name: str, allowed: bool, reason: str = "") -> Path:
        await self.refresh(force=True)
        if name not in self._loaded:
            raise LookupError(f"没有名为 {name!r} 的文件 agent(内置 agent 的 Bash 权限不可改)")
        if allowed and not reason.strip():
            raise ValueError("开启 Bash 必须写原因")
        path, _mtime = self._loaded[name]
        text = path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text, path=path)
        if allowed:
            fm["allow_exec"] = True
            fm["allow_exec_reason"] = reason.strip()
        else:
            fm.pop("allow_exec", None)
            fm.pop("allow_exec_reason", None)
        path.write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + body + "\n",
                        encoding="utf-8")
        await self.refresh(force=True)
        return path

    async def register(self, agent: Agent) -> None:
        self._table[agent.spec().component.name] = agent

    async def get(self, name: str) -> Agent | None:
        return self._table.get(name) if name in self._loaded else None

    async def list_specs(self) -> tuple[AgentSpec, ...]:
        return tuple(self._table[n].spec() for n in sorted(self._loaded) if n in self._table)
