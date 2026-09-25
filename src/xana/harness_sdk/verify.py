"""命令型验证:回合结束时执行 agent 声明的检查命令(对应 Claude Code 命令型 Stop hook)。

AGENT.md ``verify:`` 声明命令(由 agent 作者给出,模型不能增改)。每次模型以纯文本结束
一轮,harness 在工作目录依次执行;全部退出码 0 才算通过。任一非 0 或超时 → 把命令与输出
末尾作为反馈续跑(与 stop hook 共用连续拦截上限)。CC 用退出码 2 表示"有意拦截",这里是
验证语义:pytest 等失败返回 1 也必须拦下,所以非 0 一律视为未通过。
"""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from typing import Sequence

from xana.harness_sdk.contracts import VerifyCheck

OUTPUT_TAIL_CHARS = 3000


@dataclass(frozen=True)
class CheckResult:
    check: VerifyCheck
    exit_code: int | None  # None = 超时或无法启动
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


async def run_check(check: VerifyCheck, cwd: str) -> CheckResult:
    try:
        proc = await asyncio.create_subprocess_shell(
            check.cmd, cwd=cwd or None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # 独立进程组:超时时连同子进程一起结束,不留孤儿
        )
    except OSError as exc:
        return CheckResult(check, None, f"无法启动:{exc}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=check.timeout_s)
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return CheckResult(check, None, f"超时(>{check.timeout_s:g} 秒)")
    except asyncio.CancelledError:  # 用户取消本轮:检查命令连同子进程一起结束
        await _kill_group(proc)
        raise
    text = out.decode("utf-8", errors="replace")
    return CheckResult(check, proc.returncode, text[-OUTPUT_TAIL_CHARS:])


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


async def run_checks(checks: Sequence[VerifyCheck], cwd: str) -> list[CheckResult]:
    """依次执行,遇到第一个未通过即停(后面的检查通常依赖前面的)。"""
    results: list[CheckResult] = []
    for check in checks:
        result = await run_check(check, cwd)
        results.append(result)
        if not result.ok:
            break
    return results


def failure_text(result: CheckResult) -> str:
    code = "超时或无法启动" if result.exit_code is None else f"退出码 {result.exit_code}"
    return (f'Stop 验证未通过,命令 "{result.check.cmd}"({code})。输出末尾:\n'
            f"```\n{result.output_tail.strip()}\n```\n修复后再结束本轮。")


def passed_text(results: Sequence[CheckResult]) -> str:
    return "Stop 验证已通过:" + ";".join(f'"{r.check.cmd}" 退出码 0' for r in results)
