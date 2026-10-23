"""派生工具:TOOL.md 声明 → 有类型的命令工具(docs/design/iteration-agent-design.md §5.2)。

给 agent 一项执行能力而不给 Bash:声明固定命令前缀与各子命令的参数,模型只能填声明过的参数;
参数校验后拼成参数数组直接执行(不经 shell)。temporal=as_of 的工具在 ToolContext.as_of 有值时
按声明注入时点参数 / 环境变量,该参数不在模型可填范围内。

TOOL.md frontmatter:
    kind: tool
    name: qt                         小写字母开头,字母数字 _ -
    description: 一句话说明
    command: ["./qt", "--format", "json"]
    cwd: project                     project(项目目录,缺省)或绝对路径
    subcommands:                     与 args 二选一;每个子命令 {description?, args}
      income: {description: 利润表, args: {code: {type: string}, quarters: {type: integer, flag: --quarters}}}
    args: {...}                      没有子命令时的参数
    temporal: as_of                  必填:static / as_of / live
    as_of: {arg: --as-of, env: QT_AS_OF}   temporal=as_of 时必填(arg / env 至少一个)
    destructive: false               必填:是否改变外部状态
    timeout_s: 60
参数定义:type(string / integer / number / boolean)、description、flag(缺省 = 按声明顺序的位置参数)、
required(位置参数缺省必填,flag 参数缺省选填)、default、enum。正文是给模型的使用说明。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xana.component.contracts import ComponentScope, ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.builtin._process import MAX_OUTPUT_CHARS, ProcessRunner, format_exec_output, last_line
from xana.tools.contracts import ToolContext, ToolDisplay, ToolInvocation, ToolResult, ToolSpec, ToolTemporal
from xana.tools.interfaces import Tool

TOOL_DOC_NAME = "TOOL.md"
TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{1,39}")
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
_TEMPORALS = ("static", "as_of", "live")
_MAX_TIMEOUT_S = 600.0


class ToolDocError(ValueError):
    def __init__(self, path: Path, field_name: str, reason: str) -> None:
        self.path, self.field, self.reason = Path(path), field_name, reason
        super().__init__(f"{path}: [tool] {field_name}: {reason}")


@dataclass(frozen=True)
class ArgDef:
    name: str
    type: str
    description: str = ""
    flag: str | None = None          # None = 位置参数
    required: bool = False
    default: Any = None
    enum: tuple[Any, ...] = ()


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    command: tuple[str, ...]
    cwd: str                                     # "project" 或绝对路径
    temporal: ToolTemporal
    destructive: bool
    subcommands: dict[str, tuple[str, tuple[ArgDef, ...]]] = field(default_factory=dict)  # 名 → (说明, 参数)
    args: tuple[ArgDef, ...] = ()
    as_of_arg: str | None = None
    as_of_env: str | None = None
    timeout_s: float = 60.0
    usage: str = ""                              # 正文


def _args(raw: Any, path: Path, where: str) -> tuple[ArgDef, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ToolDocError(path, where, "参数必须是 mapping:参数名 → 定义")
    out: list[ArgDef] = []
    for name, d in raw.items():
        d = d or {}
        if not isinstance(d, dict):
            raise ToolDocError(path, f"{where}.{name}", "参数定义必须是 mapping")
        typ = d.get("type", "string")
        if typ not in _TYPES:
            raise ToolDocError(path, f"{where}.{name}.type", f"只支持 {'/'.join(_TYPES)}")
        flag = d.get("flag")
        if flag is not None and (not isinstance(flag, str) or not flag.startswith("-")):
            raise ToolDocError(path, f"{where}.{name}.flag", "flag 必须以 - 开头")
        if flag is None and typ == "boolean":
            raise ToolDocError(path, f"{where}.{name}", "boolean 参数必须有 flag")
        enum = d.get("enum") or ()
        out.append(ArgDef(name=str(name), type=typ, description=str(d.get("description") or ""), flag=flag,
                          required=bool(d.get("required", flag is None)), default=d.get("default"),
                          enum=tuple(enum)))
    return tuple(out)


def parse_tool_doc(fm: dict, body: str, *, path: Path) -> CommandDef:
    """TOOL.md 的 frontmatter + 正文 → CommandDef;不合法抛 ToolDocError。"""
    if fm.get("kind") != "tool":
        raise ToolDocError(path, "kind", "必须是 tool")
    name = fm.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        raise ToolDocError(path, "name", "小写字母开头,只含小写字母、数字、_ 和 -,2–40 字符")
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ToolDocError(path, "description", "必填")
    command = fm.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
        raise ToolDocError(path, "command", "必须是非空字符串数组(按参数数组执行,不经 shell)")
    temporal = fm.get("temporal")
    if temporal not in _TEMPORALS:
        raise ToolDocError(path, "temporal", f"必填:{' / '.join(_TEMPORALS)}")
    if not isinstance(fm.get("destructive"), bool):
        raise ToolDocError(path, "destructive", "必填:true / false(是否改变外部状态)")
    as_of = fm.get("as_of") or {}
    if temporal == "as_of" and not (isinstance(as_of, dict) and (as_of.get("arg") or as_of.get("env"))):
        raise ToolDocError(path, "as_of", "temporal=as_of 时必须声明 as_of.arg 或 as_of.env")
    cwd = fm.get("cwd", "project")
    if not isinstance(cwd, str) or not (cwd == "project" or Path(cwd).is_absolute()):
        raise ToolDocError(path, "cwd", "project 或绝对路径")
    timeout = fm.get("timeout_s", 60)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= _MAX_TIMEOUT_S:
        raise ToolDocError(path, "timeout_s", f"0–{_MAX_TIMEOUT_S:g} 秒")
    subs_raw = fm.get("subcommands")
    if subs_raw is not None and fm.get("args") is not None:
        raise ToolDocError(path, "subcommands", "subcommands 与 args 只能写一个")
    subs: dict[str, tuple[str, tuple[ArgDef, ...]]] = {}
    if subs_raw is not None:
        if not isinstance(subs_raw, dict) or not subs_raw:
            raise ToolDocError(path, "subcommands", "必须是非空 mapping:子命令 → {description, args}")
        for sub, d in subs_raw.items():
            d = d or {}
            if not isinstance(d, dict):
                raise ToolDocError(path, f"subcommands.{sub}", "必须是 mapping")
            subs[str(sub)] = (str(d.get("description") or ""), _args(d.get("args"), path, f"subcommands.{sub}.args"))
    return CommandDef(
        name=name, description=desc.strip(), command=tuple(command), cwd=cwd, temporal=temporal,
        destructive=fm["destructive"], subcommands=subs, args=_args(fm.get("args"), path, "args"),
        as_of_arg=as_of.get("arg") if isinstance(as_of, dict) else None,
        as_of_env=as_of.get("env") if isinstance(as_of, dict) else None,
        timeout_s=float(timeout), usage=body.strip(),
    )


def _arg_line(a: ArgDef) -> str:
    bits = [a.type, "必填" if a.required else "选填"]
    if a.default is not None:
        bits.append(f"默认 {a.default}")
    if a.enum:
        bits.append("可选 " + "/".join(map(str, a.enum)))
    return f"  - {a.name}({', '.join(bits)})" + (f":{a.description}" if a.description else "")


def _arg_schema(defs: tuple[ArgDef, ...]) -> dict:
    props = {}
    for a in defs:
        p: dict[str, Any] = {"type": a.type}
        if a.description:
            p["description"] = a.description
        if a.enum:
            p["enum"] = list(a.enum)
        props[a.name] = p
    return {"type": "object", "properties": props, "required": [a.name for a in defs if a.required]}


class CommandTool(Tool):
    """一个 TOOL.md 对应的工具实例。"""

    def __init__(self, definition: CommandDef, *, path: Path, scope: ComponentScope, project_root: Path) -> None:
        self.definition = d = definition
        self._project_root = Path(project_root)
        self._runner = ProcessRunner()
        doc = [d.description]
        if d.subcommands:
            doc.append("子命令(subcommand)及其参数(args):")
            for sub, (sdesc, args) in d.subcommands.items():
                doc.append(f"- {sub}" + (f":{sdesc}" if sdesc else ""))
                doc.extend(_arg_line(a) for a in args)
            schema: dict = {"type": "object", "properties": {
                "subcommand": {"type": "string", "enum": list(d.subcommands)},
                "args": {"type": "object", "description": "该子命令的参数(见工具说明)"}},
                "required": ["subcommand"]}
        else:
            if d.args:
                doc.append("参数:")
                doc.extend(_arg_line(a) for a in d.args)
            schema = _arg_schema(d.args)
        if d.usage:
            doc.append(d.usage)
        self._spec = ToolSpec(
            component=ComponentSpec(
                kind="tool", name=d.name, qualified_name=f"{scope}.{d.name}", description="\n".join(doc),
                source=ComponentSource(source_type=scope, scope=scope, path=Path(path))),
            input_schema=schema,
            read_only=not d.destructive,
            destructive=d.destructive,
            temporal=d.temporal,
            concurrency="serialized" if d.destructive else "parallel",
            timeout_s=d.timeout_s,
            max_result_chars=MAX_OUTPUT_CHARS,
            display=ToolDisplay(category="execute", verb=d.name, unit="次调用",
                                summary_args=("subcommand",) if d.subcommands else ()),
        )

    def spec(self) -> ToolSpec:
        return self._spec

    def argv(self, inp: dict, as_of: str | None) -> list[str]:
        """校验模型输入并拼出参数数组(不含 as_of 外的任何未声明内容);不合法抛 ValueError。"""
        d = self.definition
        if d.subcommands:
            sub = inp.get("subcommand")
            if sub not in d.subcommands:
                raise ValueError(f"subcommand 必须是 {'/'.join(d.subcommands)} 之一")
            defs = d.subcommands[sub][1]
            values = inp.get("args") or {}
            if not isinstance(values, dict):
                raise ValueError("args 必须是对象")
            head = [*d.command, sub]
        else:
            defs, values, head = d.args, inp, list(d.command)
        known = {a.name for a in defs}
        extra = sorted(set(values) - known)
        if extra:
            raise ValueError(f"未声明的参数:{', '.join(extra)}(可用:{', '.join(sorted(known)) or '无'})")
        positional: list[str] = []
        flags: list[str] = []
        for a in defs:
            v = values.get(a.name, a.default)
            if v is None:
                if a.required:
                    raise ValueError(f"缺少必填参数 {a.name}")
                continue
            if not isinstance(v, _TYPES[a.type]) or (a.type != "boolean" and isinstance(v, bool)):
                raise ValueError(f"参数 {a.name} 应为 {a.type}")
            if a.enum and v not in a.enum:
                raise ValueError(f"参数 {a.name} 只能是 {'/'.join(map(str, a.enum))}")
            if isinstance(v, str) and v.startswith("-"):
                raise ValueError(f"参数 {a.name} 的值不能以 - 开头")
            if a.type == "boolean":
                if v:
                    flags.append(a.flag or "")
            elif a.flag is None:
                positional.append(str(v))
            else:
                flags += [a.flag, str(v)]
        tail = [d.as_of_arg, as_of] if as_of and d.temporal == "as_of" and d.as_of_arg else []
        return [*head, *positional, *flags, *tail]

    async def validate(self, invocation: ToolInvocation) -> None:
        self.argv(dict(invocation.input), None)

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="declared command tool")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        d = self.definition
        try:
            argv = self.argv(dict(invocation.input), context.as_of)
        except ValueError as exc:
            return ToolResult(ok=False, content=None, error=str(exc), failure_kind="validation",
                              rendered=f"[{d.name}] 参数错误:{exc}")
        env = {d.as_of_env: context.as_of} if context.as_of and d.temporal == "as_of" and d.as_of_env else None
        cwd = str(self._project_root if d.cwd == "project" else Path(d.cwd))
        try:
            out = await self._runner.run(invocation.call_id, argv=argv, cwd=cwd, timeout_s=d.timeout_s, env=env)
        except OSError as exc:      # 命令不存在 / 不可执行
            return ToolResult(ok=False, content=None, error=str(exc), failure_kind="execution",
                              rendered=f"[{d.name}] 无法执行 {argv[0]}:{exc}")
        output = f"命令超时（{d.timeout_s:g} 秒），已终止\n{out.output}" if out.timed_out else out.output
        rendered, truncated = format_exec_output(output, out.exit_code, out.elapsed_s)
        failed = out.timed_out or out.exit_code != 0
        return ToolResult(
            ok=not failed, content={"stdout": output, "exit_code": out.exit_code},
            stdout=output, stderr=out.stderr, exit_code=out.exit_code, truncated=truncated,
            error=(None if not failed else f"超时（超过 {d.timeout_s:g} 秒）" if out.timed_out
                   else last_line(out.stderr) or last_line(output) or f"退出码 {out.exit_code}"),
            failure_kind="timeout" if out.timed_out else ("execution" if failed else None),
            rendered=rendered,
        )

    async def cancel(self, call_id: str) -> None:
        await self._runner.cancel(call_id)
