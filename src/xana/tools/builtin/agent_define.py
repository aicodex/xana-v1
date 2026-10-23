"""agent_define:查看可选工具目录 / 创建 / 删除 agent(docs/design/agent-delegation-design.md §3.4、§7)。

- catalog:工具组目录、未分组工具、已有 agent——起草 agent 前先看这里。
- create:校验后写 AGENT.md(scope=project → <project_root>/.xana/agents,user → {XANA_HOME}/agents),
  刷新注册表后立即可用。
- delete:删除文件 agent(内置不能删),立即生效。

两级 agent 目录对 Write / Edit 受保护,本工具是唯一写入口。校验:名字格式、不覆盖内置 agent、
工具名 / 组名存在、新 agent 的工具不超出调用方本 run 可用的工具(不能借创建提权)、提示词不过短。
xana 暂无交互式审批,用户确认在对话里完成(见 agent_creator 技能)。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from xana.agents.contracts import AgentSpec
from xana.agents.delegation import AGENT_DEFINE_TOOL
from xana.agents.interfaces import Agent, AgentRegistry
from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.skills.markdown_loader import SkillManifestError
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool, ToolRegistry
from xana.tools.toolsets import DEFAULT, resolve_tools, unknown_names

NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,39}")
MIN_PROMPT_CHARS = 20

_DESCRIPTION = (
    "创建 agent,或查看创建 agent 时可选的工具。创建前先用 skill_load 加载 agent_creator 技能,按其流程和用户确认草稿,"
    "再调用 create。\n"
    "- action=catalog:返回工具组目录(组名、说明、是否默认、包含的工具)、未分组工具和已有 agent。\n"
    "- action=create:写入 AGENT.md 并立即生效(可用 /agent <name> 开会话,或 agent_run 委派)。scope=project(默认)"
    "只属于当前项目,scope=user 所有项目通用;同名时项目级覆盖用户级。\n"
    "- action=delete:删除指定 name 的 agent,立即生效(内置 agent 不能删)。\n"
    "tools 是工具选择:default(默认组合)、组名、工具名或 *;[] 表示不给任何工具;不填 = default。"
    "system_prompt 是该 agent 的系统提示词,通常以 ${base_prompt} 开头(保留 xana 默认提示词与环境信息,在其后写角色说明);"
    "不含 ${base_prompt} 则完全替换,连工作目录等环境信息也没有,只在用户明确要求完全自定义时这样做。"
)

_SPEC = ToolSpec(
    temporal="static",
    destructive=True,
    toolset="agents",
    component=ComponentSpec(
        kind="tool", name=AGENT_DEFINE_TOOL, qualified_name="builtin.agent_define", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["catalog", "create", "delete"]},
            "scope": {"type": "string", "enum": ["project", "user"], "description": "create 写到哪一级;默认 project"},
            "name": {"type": "string", "description": "小写字母开头,可含数字与连字符,2–40 字符"},
            "description": {"type": "string", "description": "一句话说明这个 agent 做什么"},
            "when_to_use": {"type": "string", "description": "什么情况下用它(委派时给其他 agent 看)"},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "工具选择;不填 = default,[] = 零工具"},
            "disallowed_tools": {"type": "array", "items": {"type": "string"}},
            "system_prompt": {"type": "string"},
            "overwrite": {"type": "boolean", "description": "同名 agent 已存在时覆盖"},
        },
        "required": ["action"],
    },
    display=ToolDisplay(category="other", verb="定义 agent", summary_args=("action", "name")),
)


class AgentDefineTool(Tool):
    def __init__(self, *, roots: Mapping[str, Path], agents: Mapping[str, Agent], agent_registry: AgentRegistry,
                 tool_registry: ToolRegistry) -> None:
        self._roots = roots              # {"project": ..., "user": ...}
        self._agents = agents            # 共用的 agent 表(由 agent_registry 维护)
        self._registry = agent_registry
        self._tools = tool_registry

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if invocation.input.get("action") not in ("catalog", "create", "delete"):
            raise ValueError("action 须为 catalog、create 或 delete")
        if invocation.input.get("scope", "project") not in self._roots:
            raise ValueError("scope 须为 project 或 user")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        # 只写两级 agents 目录;内容校验与防提权在 execute 里
        return PermissionResult(decision="allow", reason="管理 agent 定义(写 agents 目录)")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        action = invocation.input["action"]
        if action == "catalog":
            await self._registry.refresh()
            return ToolResult(ok=True, content=None, rendered=await self._catalog())
        if action == "delete":
            return await self._delete(str(invocation.input.get("name") or "").strip())
        return await self._create(invocation.input, context)

    async def _delete(self, name: str) -> ToolResult:
        try:
            path = await self._registry.remove(name)
        except LookupError as exc:
            return ToolResult(ok=False, content=None, failure_kind="validation", error=str(exc),
                              rendered=f"[agent_define] 未删除:{exc}")
        still = self._agents.get(name)
        note = ""
        if still is not None:   # 删掉的是项目级,同名用户级随即生效
            note = f";同名 agent 仍存在于 {still.spec().component.source.path}"
        return ToolResult(ok=True, content={"name": name, "path": str(path)},
                          rendered=f"[agent_define] 已删除 agent {name}({path}){note}。")

    async def _catalog(self) -> str:
        specs = await self._tools.list_specs()
        lines = ["# 工具组(tools 里写组名即选中整组;default = 所有标 ✓ 的组)"]
        for ts in await self._tools.list_toolsets():
            members = sorted(sp.component.name for sp in specs if sp.toolset == ts.name)
            if members:
                mark = "✓" if ts.default else " "
                lines.append(f"- {ts.name} [{mark}] {ts.description}:{', '.join(members)}")
        loose = sorted(sp.component.name for sp in specs if not sp.toolset)
        if loose:
            lines.append(f"\n未分组工具(只能按名选):{', '.join(loose)}")
        lines.append("\n# 已有 agent")
        for name in sorted(self._agents):
            spec = self._agents[name].spec()
            kind = {"builtin": "内置", "project": "项目级", "user": "用户级"}.get(
                spec.component.source.source_type, spec.component.source.source_type)
            lines.append(f"- {name}({kind}):{spec.component.description}")
        return "\n".join(lines)

    async def _create(self, data: dict, context: ToolContext) -> ToolResult:
        name = str(data.get("name") or "").strip()
        description = str(data.get("description") or "").strip()
        prompt = str(data.get("system_prompt") or "").strip()
        tools = data.get("tools")
        disallowed = list(data.get("disallowed_tools") or [])
        specs = await self._tools.list_specs()
        toolsets = await self._tools.list_toolsets()
        errors: list[str] = []
        if not NAME_RE.fullmatch(name):
            errors.append("name 须小写字母开头,只含小写字母、数字、连字符,2–40 字符")
        existing = self._agents.get(name)
        if existing is not None and existing.spec().component.source.source_type == "builtin":
            errors.append(f"{name} 是内置 agent,不能覆盖")
        elif existing is not None and not data.get("overwrite"):
            errors.append(f"agent {name} 已存在;确认要覆盖时传 overwrite=true")
        if not description:
            errors.append("description 不能为空")
        if len(prompt) < MIN_PROMPT_CHARS:
            errors.append(f"system_prompt 至少 {MIN_PROMPT_CHARS} 个字")
        selection = tuple(tools) if tools is not None else (DEFAULT,)
        unknown = unknown_names((*selection, *disallowed), specs, toolsets)
        if unknown:
            errors.append(f"未知的组或工具:{', '.join(unknown)}(用 action=catalog 查看可选)")
        granted = resolve_tools(selection, disallowed, specs, toolsets)
        if context.available_tools is not None and not granted <= context.available_tools:
            extra = sorted(granted - context.available_tools)
            errors.append(f"新 agent 不能拥有你当前没有的工具:{', '.join(extra)}")
        if errors:
            return ToolResult(ok=False, content={"errors": errors}, failure_kind="validation",
                              error="; ".join(errors),
                              rendered="[agent_define] 未创建:\n" + "\n".join(f"- {e}" for e in errors))
        front: dict = {"kind": "agent", "name": name, "goal": description}
        if data.get("when_to_use"):
            front["when_to_use"] = str(data["when_to_use"]).strip()
        if tools is not None:
            front["tools"] = list(tools)
        if disallowed:
            front["disallowed_tools"] = disallowed
        doc = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n" + prompt + "\n"
        path = self._roots[str(data.get("scope") or "project")] / name / "AGENT.md"
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc, encoding="utf-8")
        failed = [e for e in await self._registry.refresh(force=True)
                  if isinstance(e, SkillManifestError) and Path(e.path) == path]
        if failed:  # 写出的文档加载失败:还原,不留坏文件
            if previous is None:
                path.unlink()
            else:
                path.write_text(previous, encoding="utf-8")
            await self._registry.refresh(force=True)
            return ToolResult(ok=False, content=None, failure_kind="execution", error=str(failed[0]),
                              rendered=f"[agent_define] 写入后加载失败,已还原:{failed[0]}")
        spec: AgentSpec = self._agents[name].spec()
        if spec.component.source.path != path:   # scope=user 但项目级有同名:项目级优先
            return ToolResult(ok=True, content={"name": name, "path": str(path), "shadowed_by": str(spec.component.source.path)},
                              rendered=(f"[agent_define] 已写入 {path},但项目级同名 agent({spec.component.source.path})"
                                        "优先生效;需要时先删除项目级的那个。"))
        return ToolResult(
            ok=True, content={"name": name, "path": str(path), "tools": sorted(granted)},
            rendered=(f"[agent_define] 已创建 agent {name}({path}),工具 {len(granted)} 个:"
                      f"{', '.join(sorted(granted)) or '无'}。现在可用 /agent {name} 开新会话,或用 agent_run 委派。"
                      f"\n说明:{spec.component.description}"))
