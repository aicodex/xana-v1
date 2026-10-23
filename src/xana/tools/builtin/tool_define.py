"""tool_define:派生工具的目录 / 试跑 / 注册 / 删除(docs/design/iteration-agent-design.md §5.3)。

派生工具 = TOOL.md 声明的命令工具(xana/tools/command_tool.py),给 agent 执行能力而不给 Bash。
- catalog:列出已注册的派生工具及其命令、temporal、是否有副作用。
- test:按草稿内容临时构造工具、用示例参数真实执行一次并返回输出,不注册;as_of 用于确认时点截断生效。
- create:写入 <scope>/tools/<name>/TOOL.md 并立即生效;加载失败则还原。
- delete:删除派生工具。
iterator 只能 catalog / test(注册走派生工具提议,由人决定);用户确认在对话里完成(见 tool_creator 技能)。
"""
from __future__ import annotations

import json
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.skills.markdown_loader import SkillManifestError, split_frontmatter
from xana.tools.command_tool import TOOL_DOC_NAME, CommandTool, ToolDocError, parse_tool_doc
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec
from xana.tools.file_registry import FileToolRegistry
from xana.tools.interfaces import Tool

TOOL_DEFINE_TOOL = "tool_define"
_READ_ONLY_AGENTS = frozenset({"iterator"})    # 只能起草 / 试跑,不能注册

_DESCRIPTION = (
    "管理派生工具:把一个现成的命令行程序声明成有类型参数的工具(TOOL.md),给 agent 执行能力而不给 Bash。"
    "创建前先用 skill_load 加载 tool_creator 技能,按其流程起草、试跑、和用户确认。\n"
    "- action=catalog:列出已有派生工具(命令、temporal、是否有副作用、来源文件)。\n"
    "- action=test:content 传 TOOL.md 草稿全文,input 传一组示例参数,真实执行一次并返回输出,不注册;"
    "temporal=as_of 的工具再传 as_of(过去某天,YYYY-MM-DD)确认结果确实按时点截断。"
    "有副作用(destructive: true)的草稿需要 allow_side_effects=true 才会执行。\n"
    "- action=create:content 传确认后的 TOOL.md 全文,写入并立即生效;scope=project(默认)只属于当前项目,"
    "scope=user 所有项目通用。同名已存在时需 overwrite=true。\n"
    "- action=delete:删除指定 name 的派生工具。"
)

_SPEC = ToolSpec(
    temporal="static",
    destructive=True,
    toolset="agents",
    component=ComponentSpec(
        kind="tool", name=TOOL_DEFINE_TOOL, qualified_name="builtin.tool_define", description=_DESCRIPTION,
        source=ComponentSource(source_type="builtin", scope="system")),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["catalog", "test", "create", "delete"]},
            "content": {"type": "string", "description": "TOOL.md 全文(test / create)"},
            "input": {"type": "object", "description": "test 用的示例参数(与模型调用该工具时的参数格式相同)"},
            "as_of": {"type": "string", "description": "test 时模拟的研究时点 YYYY-MM-DD"},
            "allow_side_effects": {"type": "boolean", "description": "test 有副作用的草稿时须为 true"},
            "scope": {"type": "string", "enum": ["project", "user"], "description": "create 写到哪一级;默认 project"},
            "name": {"type": "string", "description": "delete 的工具名"},
            "overwrite": {"type": "boolean", "description": "create 时同名已存在则覆盖"},
        },
        "required": ["action"],
    },
    display=ToolDisplay(category="other", verb="定义工具", summary_args=("action", "name")),
)


def _fail(msg: str, kind: str = "validation") -> ToolResult:
    return ToolResult(ok=False, content=None, error=msg, failure_kind=kind, rendered=f"[tool_define] {msg}")


class ToolDefineTool(Tool):
    def __init__(self, *, registry: FileToolRegistry, project_root: Path) -> None:
        self._registry = registry
        self._project_root = Path(project_root)

    def spec(self) -> ToolSpec:
        return _SPEC

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        if invocation.input.get("action") in ("create", "delete") and context.agent_name in _READ_ONLY_AGENTS:
            return PermissionResult(decision="deny", reason=(
                f"{context.agent_name} 不能注册或删除工具:把 TOOL.md 草稿与理由写进派生工具提议,由用户决定"))
        return PermissionResult(decision="allow", reason="tool_define")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        inp = invocation.input
        action = inp.get("action")
        if action == "catalog":
            return await self._catalog()
        if action == "delete":
            try:
                path = await self._registry.remove(str(inp.get("name") or ""))
            except LookupError as exc:
                return _fail(str(exc), "not_found")
            return ToolResult(ok=True, content={"deleted": str(path)}, rendered=f"[tool_define] 已删除 {path}")
        content = str(inp.get("content") or "")
        if not content.strip():
            return _fail("content 不能为空:传 TOOL.md 全文")
        if action == "test":
            return await self._test(content, inp, invocation, context)
        if action == "create":
            return await self._create(content, inp)
        return _fail("action 必须是 catalog / test / create / delete")

    def _draft(self, content: str) -> CommandTool:
        path = self._project_root / ".xana" / "tools" / "_draft" / TOOL_DOC_NAME
        try:
            fm, body = split_frontmatter(content, path=path)
        except SkillManifestError as exc:
            raise ToolDocError(path, exc.field, exc.reason) from exc
        return CommandTool(parse_tool_doc(fm, body, path=path), path=path, scope="project",
                           project_root=self._project_root)

    async def _catalog(self) -> ToolResult:
        tools = await self._registry.file_tools()
        if not tools:
            return ToolResult(ok=True, content=[], rendered="还没有派生工具。")
        lines = []
        for t in tools:
            d = t.definition
            lines.append(f"- {d.name}:{d.description}\n  命令 {json.dumps(list(d.command), ensure_ascii=False)}"
                         f" · temporal={d.temporal} · {'有副作用' if d.destructive else '无副作用'}"
                         f" · {t.spec().component.source.path}")
        return ToolResult(ok=True, content=[t.definition.name for t in tools], rendered="\n".join(lines))

    async def _test(self, content: str, inp: dict, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        try:
            tool = self._draft(content)
        except ToolDocError as exc:
            return _fail(f"草稿不合法:{exc.field}:{exc.reason}")
        if tool.definition.destructive and not inp.get("allow_side_effects"):
            return _fail("草稿声明了副作用(destructive: true);确认可以真实执行后传 allow_side_effects=true")
        example = inp.get("input") or {}
        if not isinstance(example, dict):
            return _fail("input 必须是对象")
        as_of = str(inp.get("as_of") or "").strip() or None
        try:
            argv = tool.argv(example, as_of)
        except ValueError as exc:
            return _fail(f"示例参数不合法:{exc}")
        run_ctx = ToolContext(session_id=context.session_id, agent_name=context.agent_name, cwd=context.cwd,
                              permission=context.permission, run_id=context.run_id, as_of=as_of)
        result = await tool.execute(ToolInvocation(
            tool_name=tool.definition.name, input=example, call_id=f"{invocation.call_id}:test",
            session_id=invocation.session_id, run_id=invocation.run_id), run_ctx)
        header = f"[tool_define test] 执行 {json.dumps(argv, ensure_ascii=False)}" + (f"(as_of={as_of})" if as_of else "")
        return ToolResult(ok=result.ok, content={"argv": argv, "ok": result.ok}, error=result.error,
                          failure_kind=result.failure_kind, rendered=f"{header}\n{result.rendered}")

    async def _create(self, content: str, inp: dict) -> ToolResult:
        try:
            tool = self._draft(content)
        except ToolDocError as exc:
            return _fail(f"草稿不合法:{exc.field}:{exc.reason}")
        name = tool.definition.name
        roots = self._registry.roots
        scope = str(inp.get("scope") or "project")
        if scope not in roots:
            return _fail("scope 必须是 project / user")
        if (existing := await self._registry.get(name)) is not None:
            if existing.spec().component.source.source_type == "builtin" or not isinstance(existing, CommandTool):
                return _fail(f"{name} 是内置工具,不能覆盖")
            if not inp.get("overwrite"):
                return _fail(f"工具 {name} 已存在;确认要覆盖时传 overwrite=true")
        path = roots[scope] / name / TOOL_DOC_NAME
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        failed = [e for e in await self._registry.refresh(force=True) if e.path == path]
        if failed:
            if previous is None:
                path.unlink()
            else:
                path.write_text(previous, encoding="utf-8")
            await self._registry.refresh(force=True)
            return _fail(f"写入后加载失败,已还原:{failed[0]}", "execution")
        live = await self._registry.get(name)
        if live is None or live.spec().component.source.path != path:
            shadow = live.spec().component.source.path if live is not None else None
            return ToolResult(ok=True, content={"name": name, "path": str(path), "shadowed_by": str(shadow)},
                              rendered=f"[tool_define] 已写入 {path},但同名工具 {shadow} 优先生效。")
        return ToolResult(ok=True, content={"name": name, "path": str(path)}, rendered=(
            f"[tool_define] 已注册工具 {name}({path})。agent 在 AGENT.md 的 tools 里写上 {name} 即可使用。"))
