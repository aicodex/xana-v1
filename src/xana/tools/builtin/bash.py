"""Bash: run a shell command, capture stdout/stderr/exit_code.

v1 uses ``asyncio.create_subprocess_shell`` so the model can pass a single
command string with pipes/redirection (matches CC ergonomics). Optional
``timeout`` (default 120s, max 600s) kills the subprocess with
``failure_kind="timeout"`` and keeps the output produced so far.

给模型的结果照 Codex(format_exec_output_for_model):退出码、用时,输出超长时注明总行数,
然后是 stdout / stderr 按到达顺序合并的输出;超长时保留首尾、省略中间(报错多在末尾)。
退出码语义照 CC(commandSemantics.ts):grep / rg / find / diff / test 退出码 1 不算失败。
"""
from __future__ import annotations

import re

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.builtin._process import MAX_OUTPUT_CHARS as _MAX_OUTPUT_CHARS
from xana.tools.builtin._process import ProcessRunner, format_exec_output, last_line
from xana.tools.interfaces import Tool
from xana.state.process_logs import ProcessLogs


_DEFAULT_TIMEOUT_S = 120.0
_MAX_TIMEOUT_S = 600.0
_MS_HEURISTIC_THRESHOLD = 600  # values above this for the "timeout" alias are treated as ms


def _resolve_timeout_s(input_: dict) -> float:
    """Return the effective timeout in seconds from a tool input dict.

    ``timeout_s`` is always treated as seconds.  ``timeout`` (alias) is treated
    as milliseconds when its value exceeds ``_MS_HEURISTIC_THRESHOLD`` (600),
    because models sometimes send milliseconds (e.g. ``{"timeout": 5000}``).
    """
    if input_.get("timeout_s") is not None:
        return float(input_["timeout_s"])
    raw = input_.get("timeout")
    if raw is None:
        return _DEFAULT_TIMEOUT_S
    raw = float(raw)
    if raw > _MS_HEURISTIC_THRESHOLD:
        return raw / 1000.0
    return raw


_SPEC = ToolSpec(
    temporal="live",
    toolset="exec",
    component=ComponentSpec(
        kind="tool",
        name="Bash",
        qualified_name="builtin.Bash",
        description="运行一条 shell 命令,返回退出码、用时和输出(stdout 与 stderr 按产生顺序合并)。",
        source=ComponentSource(source_type="builtin", scope="system"),
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要运行的命令"},
            "description": {"type": "string", "description": "用几个字说明这条命令做什么(展示给用户)"},
            "timeout_s": {"type": "number", "description": "超时秒数"},
        },
        "required": ["command"],
    },
    destructive=True,
    open_world=True,
    max_result_chars=_MAX_OUTPUT_CHARS,
    display=ToolDisplay(category="execute", verb="运行", unit="条命令", summary_args=("description", "command")),
)


class BashTool(Tool):
    # 活跃子进程按 execution_id 登记；provider 的 call_id 可重复。

    def __init__(self, *, logs: ProcessLogs | None = None) -> None:
        self._runner = ProcessRunner()
        self._logs = logs

    def spec(self) -> ToolSpec:
        return _SPEC

    async def validate(self, invocation: ToolInvocation) -> None:
        if not isinstance(invocation.input.get("command"), str) or not invocation.input["command"].strip():
            raise ValueError("Bash requires non-empty 'command'")

    async def check_permission(self, invocation: ToolInvocation, context: ToolContext) -> PermissionResult:
        return PermissionResult(decision="allow", reason="bash allowed in v1; gating delegated to caller")

    async def execute(self, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        command = invocation.input["command"]
        timeout = min(_resolve_timeout_s(invocation.input), _MAX_TIMEOUT_S)
        logs = self._logs
        if logs is not None:
            logs.start(invocation.execution_id, session_id=context.session_id, run_id=context.run_id or "",
                       call_id=invocation.call_id, command=command)
        out = None
        try:
            out = await self._runner.run(invocation.execution_id, shell=command, cwd=context.cwd, timeout_s=timeout,
                on_output=(lambda channel, text: logs.append(invocation.execution_id, channel, text)) if logs else None)
        finally:
            if logs is not None:
                logs.finish(invocation.execution_id, out.exit_code if out else None)
        output = out.output
        failed, note = (True, "") if out.timed_out else _interpret_exit(command, out.exit_code)
        if out.timed_out:
            output = f"命令超时（{timeout:g} 秒），已终止\n{output}"
        rendered, truncated = format_exec_output(output, out.exit_code, out.elapsed_s, note=note)
        error = None
        if failed:
            error = (f"超时（超过 {timeout:g} 秒）" if out.timed_out
                     else last_line(out.stderr) or last_line(output) or f"退出码 {out.exit_code}")
        return ToolResult(
            ok=not failed,
            content={"stdout": output, "stderr": out.stderr, "exit_code": out.exit_code},
            stdout=output,
            stderr=out.stderr,
            exit_code=out.exit_code,
            truncated=truncated,
            metadata={"log_available": logs is not None},
            error=error,
            failure_kind="timeout" if out.timed_out else ("execution" if failed else None),
            rendered=rendered,
        )

    async def cancel(self, call_id: str) -> None:
        await self._runner.cancel(call_id)


# 退出码 1 不表示出错的命令(照 CC commandSemantics.ts);值 = 给模型的说明
_EXIT_1_OK = {"grep": "没有匹配", "rg": "没有匹配", "find": "部分目录无法访问",
              "diff": "文件有差异", "test": "条件为假", "[": "条件为假"}


def _interpret_exit(command: str, exit_code: int) -> tuple[bool, str]:
    """(是否失败, 退出码说明)。按最后一段命令判断(它决定整条命令的退出码),同 CC 的启发式。"""
    if exit_code == 0:
        return False, ""
    last = [seg for seg in re.split(r"\|\||&&|;|\|", command) if seg.strip()]
    base = last[-1].split()[0] if last else ""
    if exit_code == 1 and base in _EXIT_1_OK:
        return False, f"{_EXIT_1_OK[base]}，不表示出错"
    return True, ""
