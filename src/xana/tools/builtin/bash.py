"""Bash: run a shell command, capture stdout/stderr/exit_code.

v1 uses ``asyncio.create_subprocess_shell`` so the model can pass a single
command string with pipes/redirection (matches CC ergonomics). Output
beyond ``max_result_chars`` is truncated and the ``truncated`` flag is
set. Optional ``timeout`` (default 120s, max 600s) kills the subprocess
with ``failure_kind="timeout"``.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.policy.contracts import PermissionResult
from xana.tools.contracts import ToolDisplay, ToolContext, ToolInvocation, ToolResult, ToolSpec
from xana.tools.interfaces import Tool


_DEFAULT_TIMEOUT_S = 120.0
_MAX_TIMEOUT_S = 600.0
_MAX_OUTPUT_CHARS = 50_000
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
    toolset="exec",
    component=ComponentSpec(
        kind="tool",
        name="Bash",
        qualified_name="builtin.Bash",
        description="运行一条 shell 命令,返回 stdout、stderr 和退出码。",
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
    # write 类工具:内部维护活跃子进程 + 2s grace。cancel() 发 SIGTERM,2s 未退则 SIGKILL。
    _GRACE_S = 2.0

    def __init__(self) -> None:
        # call_id → 活跃子进程。execute 进入即登记,完成即清。
        self._procs: dict[str, asyncio.subprocess.Process] = {}

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

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=context.cwd,
        )
        self._procs[invocation.call_id] = proc
        try:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    ok=False, content=None,
                    error=f"timeout after {timeout}s",
                    failure_kind="timeout",
                    rendered=f"[Bash timeout] command exceeded {timeout}s",
                    exit_code=-1,
                )
            except asyncio.CancelledError:
                # 外层 task.cancel() 兜底强杀(grace 已过期或 read_only 路径)。
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise
        finally:
            self._procs.pop(invocation.call_id, None)

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        truncated = False
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
            truncated = True
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
            truncated = True

        exit_code = proc.returncode if proc.returncode is not None else -1
        rendered = _render_bash_result(stdout, stderr, exit_code, truncated)
        return ToolResult(
            ok=exit_code == 0,
            content={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            truncated=truncated,
            failure_kind=None if exit_code == 0 else "execution",
            rendered=rendered,
        )

    async def cancel(self, call_id: str) -> None:
        """SIGTERM → 等 grace → SIGKILL。子进程退出后 execute 的 wait_for/
        communicate 会自然返回,ToolExecutor 拿到带 exit_code 的结果。"""
        proc = self._procs.get(call_id)
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._GRACE_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def _render_bash_result(stdout: str, stderr: str, exit_code: int, truncated: bool) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")
    if exit_code != 0:
        parts.append(f"[exit {exit_code}]")
    if truncated:
        parts.append("[output truncated]")
    return "\n".join(parts) if parts else f"[Bash ok] (no output, exit {exit_code})"
