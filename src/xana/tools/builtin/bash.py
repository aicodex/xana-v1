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

import asyncio
import contextlib
import os
import re
import signal
import time

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

        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=context.cwd,
            start_new_session=True,      # 自成进程组:超时 / 取消时连同子进程一起结束,不留进程占着管道
        )
        self._procs[invocation.call_id] = proc
        combined: list[str] = []          # stdout / stderr 按到达顺序
        err_chunks: list[str] = []
        readers = asyncio.gather(_pump(proc.stdout, combined), _pump(proc.stderr, combined, err_chunks))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(asyncio.shield(readers), timeout=timeout)
                await proc.wait()
            except asyncio.TimeoutError:
                timed_out = True
                _signal_group(proc, signal.SIGKILL)
                await proc.wait()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(readers, timeout=1.0)
            except asyncio.CancelledError:
                # 外层 task.cancel() 兜底强杀(grace 已过期或 read_only 路径)。
                _signal_group(proc, signal.SIGKILL)
                await proc.wait()
                readers.cancel()
                raise
        finally:
            self._procs.pop(invocation.call_id, None)

        elapsed = time.monotonic() - started
        output = "".join(combined)
        stderr = "".join(err_chunks)
        exit_code = -1 if timed_out or proc.returncode is None else proc.returncode
        failed, note = (True, "") if timed_out else _interpret_exit(command, exit_code)
        if timed_out:
            output = f"命令超时（{timeout:g} 秒），已终止\n{output}"
        rendered, truncated = format_exec_output(output, exit_code, elapsed, note=note)
        error = None
        if failed:
            error = (f"超时（超过 {timeout:g} 秒）" if timed_out
                     else _last_line(stderr) or _last_line(output) or f"退出码 {exit_code}")
        return ToolResult(
            ok=not failed,
            content={"stdout": output, "stderr": stderr, "exit_code": exit_code},
            stdout=output,
            stderr=stderr,
            exit_code=exit_code,
            truncated=truncated,
            error=error,
            failure_kind="timeout" if timed_out else ("execution" if failed else None),
            rendered=rendered,
        )

    async def cancel(self, call_id: str) -> None:
        """SIGTERM → 等 grace → SIGKILL(整个进程组)。子进程退出后 execute 自然返回,
        ToolExecutor 拿到带 exit_code 的结果。"""
        proc = self._procs.get(call_id)
        if proc is None or proc.returncode is not None:
            return
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._GRACE_S)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            await proc.wait()


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def _pump(stream: asyncio.StreamReader | None, combined: list[str],
                also: list[str] | None = None) -> None:
    """按块读管道,解码后追加到 combined(两路共用 → 保持到达顺序);also 另存一份(stderr)。"""
    if stream is None:
        return
    while chunk := await stream.read(65536):
        text = chunk.decode("utf-8", errors="replace")
        combined.append(text)
        if also is not None:
            also.append(text)


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


def format_exec_output(output: str, exit_code: int, elapsed_s: float, *, note: str = "") -> tuple[str, bool]:
    """给模型的命令结果(结构照 Codex format_exec_output_for_model),返回 (文本, 是否截断)。"""
    total_lines = output.count("\n") + (0 if output.endswith("\n") or not output else 1)
    body = output.rstrip("\n")
    truncated = len(body) > _MAX_OUTPUT_CHARS
    if truncated:
        half = _MAX_OUTPUT_CHARS // 2
        head, tail = body[:half], body[-half:]
        omitted = body[half:-half].count("\n")
        body = f"{head}\n\n…[中间省略 {omitted} 行]…\n\n{tail}"
    lines = [f"退出码：{exit_code}" + (f"（{note}）" if note else ""), f"用时：{elapsed_s:.1f} 秒"]
    if truncated:
        lines.append(f"输出总行数：{total_lines}")
    lines.append("输出：" if body else "输出：（无）")
    if body:
        lines.append(body)
    return "\n".join(lines), truncated


def _last_line(text: str) -> str:
    """最后一个非空行(Python traceback 等的错误摘要通常在这里)。"""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""
