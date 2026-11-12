"""子进程执行与结果格式(Bash 与派生命令工具共用)。

- 子进程自成进程组:超时 / 取消时连同子进程一起结束,不留进程占着管道。
- stdout / stderr 按到达顺序合并;超时保留已有输出。
- 给模型的结果照 Codex(format_exec_output_for_model):退出码、用时,输出超长时注明总行数并保留首尾。
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Literal
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 50_000
_GRACE_S = 2.0


@dataclass(frozen=True)
class ExecOutcome:
    output: str          # stdout + stderr 按到达顺序
    stderr: str
    exit_code: int       # 超时为 -1
    timed_out: bool
    elapsed_s: float


class ProcessRunner:
    """按单次执行身份登记活跃子进程，支持协作式取消。"""

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, key: str, *, shell: str | None = None, argv: Sequence[str] | None = None,
                  cwd: str, timeout_s: float, env: Mapping[str, str] | None = None,
                  on_output: Callable[[Literal["stdout", "stderr"], str], None] | None = None) -> ExecOutcome:
        """shell 与 argv 二选一:shell 经 /bin/sh 解释;argv 直接执行,不经 shell。"""
        started = time.monotonic()
        full_env = {**os.environ, **env} if env else None
        common = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
                      env=full_env, start_new_session=True)
        if shell is not None:
            proc = await asyncio.create_subprocess_shell(shell, **common)
        else:
            assert argv, "argv 不能为空"
            proc = await asyncio.create_subprocess_exec(*argv, **common)
        self._procs[key] = proc
        combined: list[str] = []
        errors: list[str] = []
        readers = asyncio.gather(_pump(proc.stdout, combined, on_output=on_output),
            _pump(proc.stderr, combined, errors, channel="stderr", on_output=on_output))
        timed_out = False
        try:
            try:
                deadline = started + timeout_s
                await asyncio.wait_for(asyncio.shield(readers), timeout=max(0.0, deadline - time.monotonic()))
                await asyncio.wait_for(proc.wait(), timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                timed_out = True
                _signal_group(proc, signal.SIGKILL)
                await proc.wait()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(readers, timeout=1.0)
            except BaseException:
                # 外层 task.cancel() 兜底强杀(grace 已过期或 read_only 路径)。
                _signal_group(proc, signal.SIGKILL)
                await proc.wait()
                readers.cancel()
                await asyncio.gather(readers, return_exceptions=True)
                raise
        finally:
            self._procs.pop(key, None)
        exit_code = -1 if timed_out or proc.returncode is None else proc.returncode
        return ExecOutcome(output="".join(combined), stderr="".join(errors), exit_code=exit_code,
                           timed_out=timed_out, elapsed_s=time.monotonic() - started)

    async def cancel(self, key: str) -> None:
        """SIGTERM → 等 grace → SIGKILL(整个进程组)。子进程退出后 run 自然返回。"""
        proc = self._procs.get(key)
        if proc is None or proc.returncode is not None:
            return
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_GRACE_S)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            await proc.wait()


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def _pump(stream: asyncio.StreamReader | None, combined: list[str],
                also: list[str] | None = None, *, channel: Literal["stdout", "stderr"] = "stdout",
                on_output: Callable[[Literal["stdout", "stderr"], str], None] | None = None) -> None:
    """按块读管道,解码后追加到 combined(两路共用 → 保持到达顺序);also 另存一份(stderr)。"""
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(65536)
        text = decoder.decode(chunk, final=not chunk)
        if text:
            combined.append(text)
            if also is not None:
                also.append(text)
            if on_output is not None:
                on_output(channel, text)
        if not chunk:
            return


def format_exec_output(output: str, exit_code: int, elapsed_s: float, *, note: str = "") -> tuple[str, bool]:
    """给模型的命令结果(结构照 Codex format_exec_output_for_model),返回 (文本, 是否截断)。"""
    total_lines = output.count("\n") + (0 if output.endswith("\n") or not output else 1)
    body = output.rstrip("\n")
    truncated = len(body) > MAX_OUTPUT_CHARS
    if truncated:
        half = MAX_OUTPUT_CHARS // 2
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


def last_line(text: str) -> str:
    """最后一个非空行(Python traceback 等的错误摘要通常在这里)。"""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""
