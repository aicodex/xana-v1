"""project_manage:在聊天里管理项目(docs/design/project-design.md §7),只在 default 项目注册。

一个项目一个 serve、一个飞书机器人。动作:list / create / configure / bind_feishu / feishu_status / start / stop。
飞书机器人用"扫码创建应用"(设备码流程)接入:bind_feishu 立即返回授权链接并在后台轮询,
确认后把机器人凭据写进该项目配置,并把确认人加入该项目的配对白名单。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.config.paths import DEFAULT_PROJECT
from xana.policy.contracts import PermissionResult
from xana.runtime import projects
from xana.skills.contracts import SkillActivation
from xana.skills.interfaces import SkillRegistry
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool, ToolRegistry
from xana.tools.toolsets import unknown_names

PROJECT_MANAGE_TOOL = "project_manage"
DEFAULT_WAIT_S = 120.0
DEFAULTS_KEYS = ("tools", "disallowed_tools", "skills")

_DESCRIPTION = (
    "管理 xana 项目:一个项目 = 一套记忆 + 自己的 agent / 技能 / 工具默认配置,由一个独立的 serve 运行,"
    "通常接一个自己的飞书机器人。按 project_creator 技能的流程和用户确认后再创建。\n"
    "- list:列出项目、目录、端口、是否在运行、是否已接飞书机器人。\n"
    "- create:name(小写字母或数字开头,可含 - _);root 可选(缺省放在 ~/.xana/projects/<name>/workspace);"
    "agent_defaults 可选:{tools, disallowed_tools, skills},写法同 agent 的工具 / 技能选择"
    "(tools 缺省 [\"default\"],skills 缺省全部,[] 表示没有);instructions 可选:项目说明(Markdown),"
    "写进项目根目录的 AGENTS.md,该项目的每个会话都会读到(目录里已有 AGENTS.md 则不覆盖)。\n"
    "- configure:修改 name 项目的 agent_defaults;项目在运行时需要 stop 再 start 才生效。\n"
    "- bind_feishu:为 name 项目创建并接入一个新的飞书机器人,返回授权链接。把链接发给用户;"
    "用户在飞书里点开确认后,项目会自动启动(已在运行的会重启以连接新机器人)。\n"
    f"- feishu_status:等待并返回绑定结果(最多约 {int(DEFAULT_WAIT_S // 60)} 分钟)。\n"
    "- start / stop:启动 / 停止 name 项目的 serve。default 项目不能在这里停止。"
)

_SPEC = ToolSpec(
    toolset="projects",
    component=ComponentSpec(
        kind="tool", name=PROJECT_MANAGE_TOOL, qualified_name="builtin.project_manage", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["list", "create", "configure", "bind_feishu", "feishu_status", "start", "stop"]},
            "name": {"type": "string", "description": "项目名(除 list 外必填)"},
            "root": {"type": "string", "description": "create:项目目录,可选"},
            "instructions": {"type": "string", "description": "create:项目说明,写进 AGENTS.md,可选"},
            "agent_defaults": {
                "type": "object", "description": "create / configure:agent 默认配置",
                "properties": {"tools": {"type": "array", "items": {"type": "string"}},
                               "disallowed_tools": {"type": "array", "items": {"type": "string"}},
                               "skills": {"type": "array", "items": {"type": "string"}}},
            },
        },
        "required": ["action"],
    },
    open_world=True,
    display=ToolDisplay(category="other", verb="管理项目", summary_args=("action", "name")),
)


@dataclass
class _Binding:
    url: str
    task: asyncio.Task
    result: dict[str, Any] = field(default_factory=dict)


class ProjectManageTool(Tool):
    def __init__(self, *, tool_registry: ToolRegistry, skills: SkillRegistry,
                 begin: Callable[[], dict] | None = None,
                 poll: Callable[[dict], dict | None] | None = None,
                 probe: Callable[[dict], dict] | None = None,
                 start: Callable[[str], int] | None = None) -> None:
        """begin / poll / probe:飞书扫码注册三步(缺省用 feishu_onboard,测试可替换);start:启动项目 serve
        (缺省 projects.start_project)。"""
        self._start = start or projects.start_project
        from xana.agent_adapter.channels import feishu_onboard as fo
        self._tools = tool_registry
        self._skills = skills
        self._begin = begin or (lambda: fo.begin_registration())
        self._poll = poll or (lambda b: fo.poll_registration(
            device_code=b["device_code"], interval=b["interval"], expire_in=b["expire_in"]))
        self._probe = probe or (lambda c: fo.probe_bot(c["app_id"], c["app_secret"], c["domain"]))
        self._bindings: dict[str, _Binding] = {}

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if invocation.input.get("action") != "list" and not str(invocation.input.get("name") or "").strip():
            raise ValueError("name 必填")
        defaults = invocation.input.get("agent_defaults")
        if defaults is not None and set(defaults) - set(DEFAULTS_KEYS):
            raise ValueError(f"agent_defaults 只能有 {list(DEFAULTS_KEYS)}")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="管理 xana 项目(default 项目的管理台)")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        action = invocation.input["action"]
        name = str(invocation.input.get("name") or "").strip()
        try:
            if action == "list":
                return _ok(self._render_list())
            if action == "create":
                return await self._create(name, invocation.input)
            if action == "configure":
                defaults = await self._check_defaults(invocation.input.get("agent_defaults") or {})
                projects.set_agent_defaults(name, defaults)
                note = "(项目正在运行:stop 再 start 后生效)" if projects.running_pid(name) else ""
                return _ok(f"[project_manage] 已更新 {name} 的 agent 默认配置:{defaults}{note}")
            if action == "bind_feishu":
                return await self._bind_feishu(name)
            if action == "feishu_status":
                return await self._feishu_status(name, context)
            if action == "start":
                pid = projects.start_project(name)
                return _ok(f"[project_manage] 已启动项目 {name}(pid {pid});启动约需一分钟,之后即可在它的机器人里聊天。")
            if action == "stop":
                if name == DEFAULT_PROJECT:
                    return _fail("default 项目是管理台,不能在这里停止(需要时用命令行)")
                stopped = projects.stop_project(name)
                return _ok(f"[project_manage] {'已停止' if stopped else '本来就没在运行'}项目 {name}")
        except (ValueError, LookupError, FileExistsError, RuntimeError) as exc:
            return _fail(str(exc))
        return _fail(f"未知 action {action!r}")

    def _render_list(self) -> str:
        lines = ["[project_manage] 项目:"]
        for p in projects.all_projects():
            state = f"运行中(pid {p.running_pid})" if p.running_pid else "未运行"
            bot = f"飞书机器人 {p.feishu_bot}" if p.feishu_bot else "未接机器人"
            lines.append(f"- {p.id}:{state} · {bot} · 端口 {p.port} · 目录 {p.root}"
                         + (f" · agent 默认 {p.agent_defaults}" if p.agent_defaults else ""))
        return "\n".join(lines)

    async def _create(self, name: str, data: dict) -> ToolResult:
        defaults = await self._check_defaults(data.get("agent_defaults") or {})
        root = Path(str(data["root"])) if data.get("root") else None
        info = projects.create_project(name, root=root, agent_defaults=defaults or None)
        instructions = str(data.get("instructions") or "").strip()
        note = ""
        if instructions:
            note = (",项目说明已写入 AGENTS.md" if projects.write_instructions(info.root, instructions)
                    else "(目录里已有 AGENTS.md,没有覆盖)")
        return _ok(f"[project_manage] 已创建项目 {info.id}:目录 {info.root},端口 {info.port}"
                   + (f",agent 默认 {defaults}" if defaults else "") + note
                   + "。下一步:bind_feishu 接一个飞书机器人,然后 start。")

    async def _check_defaults(self, defaults: dict) -> dict:
        """名字须是已知的工具 / 组 / 技能;返回规整后的 agent_defaults。"""
        out: dict = {}
        tool_items = [*defaults.get("tools", []), *defaults.get("disallowed_tools", [])]
        bad = unknown_names(tool_items, await self._tools.list_specs(), await self._tools.list_toolsets())
        activation = SkillActivation(kind="system", session_id="", agent_name="", run_id="")
        skill_names = {s.spec().component.name for s in await self._skills.discover(activation)}
        bad += sorted(set(defaults.get("skills") or []) - skill_names - {"*"})
        if bad:
            raise ValueError(f"未知的工具 / 组 / 技能:{', '.join(bad)}")
        for key in DEFAULTS_KEYS:
            if key in defaults:
                out[key] = list(defaults[key])
        return out

    async def _bind_feishu(self, name: str) -> ToolResult:
        info = projects.project_info(name)          # 未注册抛 LookupError
        current = self._bindings.get(name)
        if current is not None and not current.task.done():
            return _ok(f"[project_manage] {name} 已有进行中的绑定,授权链接:{current.url}")
        begin = await asyncio.to_thread(self._begin)
        binding = _Binding(url=begin["qr_url"], task=asyncio.create_task(self._finish_binding(name, begin)))
        self._bindings[name] = binding
        replace = f"(将替换已接的机器人 {info.feishu_bot})" if info.feishu_bot else ""
        return _ok(f"[project_manage] 请把这个授权链接发给用户{replace}:{binding.url}\n"
                   f"用户在飞书里点开并确认创建机器人后(约 {begin['expire_in'] // 60} 分钟内有效),项目会自动启动,"
                   "约一分钟后用户就能直接在新机器人里聊天;用户回来时可调用 feishu_status 确认结果。")

    async def _finish_binding(self, name: str, begin: dict) -> None:
        binding_result: dict[str, Any] = {}
        try:
            creds = await asyncio.to_thread(self._poll, begin)
            if creds is None:
                binding_result.update(status="failed", error="用户未确认或链接已过期")
            else:
                creds.update(await asyncio.to_thread(self._probe, creds))
                projects.set_feishu_bot(name, creds)
                binding_result.update(status="done", bot=creds.get("bot_name") or creds["app_id"])
                # 用户确认后就会直接去新机器人里发消息:不等模型再调 start,绑定完成即(重)启动该项目
                binding_result["pid"] = await asyncio.to_thread(self._restart, name)
        except Exception as exc:  # 网络 / 飞书接口错误:记下来,由 feishu_status 报告
            binding_result.update(status="failed", error=str(exc))
        self._bindings[name].result.update(binding_result)

    def _restart(self, name: str) -> int:
        if projects.running_pid(name):
            projects.stop_project(name)          # 换了机器人:重启才会连新机器人
        return self._start(name)

    async def _feishu_status(self, name: str, context: ToolContext) -> ToolResult:
        binding = self._bindings.get(name)
        if binding is None:
            return _fail(f"{name} 没有进行中的飞书绑定,先调用 bind_feishu")
        if context.report_progress is not None and not binding.task.done():
            context.report_progress("等待用户在飞书里确认…")
        try:
            await asyncio.wait_for(asyncio.shield(binding.task), timeout=DEFAULT_WAIT_S)
        except asyncio.TimeoutError:
            return _ok(f"[project_manage] 还在等待用户确认;授权链接:{binding.url}")
        result = binding.result
        if result.get("status") != "done":
            return _fail(f"飞书绑定失败:{result.get('error')}")
        return _ok(f"[project_manage] 已为 {name} 接入飞书机器人 {result['bot']},用户已是它的授权用户;"
                   f"项目已自动启动(pid {result['pid']}),约一分钟后即可在新机器人里聊天。")


def _ok(text: str) -> ToolResult:
    return ToolResult(ok=True, content=None, rendered=text)


def _fail(text: str) -> ToolResult:
    return ToolResult(ok=False, content=None, failure_kind="validation", error=text,
                      rendered=f"[project_manage] {text}")
