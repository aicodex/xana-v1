"""Local POSIX ExecutionEnvironment with process-group isolation, timeout/orphan cleanup, and diag logging."""
from __future__ import annotations

import asyncio
import os
import resource
import signal
import time
import uuid
from typing import Callable

from xana.policy.contracts import ProcessOutcome, ProcessSpec
from xana.policy.interfaces import AbortLike, ExecutionEnvironment
from xana.policy.tape_recorder import PolicyTapeRecorder
from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion

_DIAG = DiagLogger("env")
_GRACE_S = 2.0


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def _trim(raw: bytes, limit: int) -> tuple[str, bool]:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]", True


def _make_rlimit_preexec(memory_limit_bytes: int | None) -> Callable[[], None] | None:
    if memory_limit_bytes is None or memory_limit_bytes <= 0:
        return None
    limit = int(memory_limit_bytes)

    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


class LocalProcessEnvironment(ExecutionEnvironment):
    """POSIX subprocess execution environment with process-group cleanup and optional RLIMIT_AS."""

    ENV_VERSION = "1.0.0"

    def __init__(self, *, recorder: PolicyTapeRecorder | None = None, slow_ms: float = 5000.0) -> None:
        self._recorder = recorder or PolicyTapeRecorder(None)
        self._slow_ms = slow_ms
        self._active: dict[str, asyncio.subprocess.Process] = {}

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="local_process_env",
            kind="exec_env",
            version=self.ENV_VERSION,
            policy_version=self.ENV_VERSION,
        )

    async def cancel(self, exec_id: str) -> None:
        proc = self._active.get(exec_id)
        if proc is None or proc.returncode is not None:
            return
        _DIAG.anomaly("exec.cancel", exec_id=exec_id, pid=proc.pid)
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_GRACE_S)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            await proc.wait()

    async def run_process(
        self,
        spec: ProcessSpec,
        *,
        abort: AbortLike | None = None,
    ) -> ProcessOutcome:
        if (spec.shell is None) == (spec.argv is None):
            raise ValueError("ProcessSpec requires exactly one of `shell` or `argv`")
        exec_id = spec.exec_id or uuid.uuid4().hex[:12]
        env = {**os.environ, **spec.env} if spec.env else None
        preexec = _make_rlimit_preexec(spec.memory_limit_bytes)
        t0 = time.monotonic()

        if spec.shell is not None:
            proc = await asyncio.create_subprocess_shell(
                spec.shell,
                cwd=spec.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,  # type: ignore[misc]
                cwd=spec.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
            )

        self._active[exec_id] = proc
        timed_out = False
        cancelled = False
        comm_task = asyncio.create_task(proc.communicate())
        abort_task = asyncio.create_task(abort.wait()) if abort is not None else None
        try:
            waiters = {comm_task} | ({abort_task} if abort_task is not None else set())
            done, pending = await asyncio.wait(waiters, timeout=spec.timeout_s, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                timed_out = True
                _signal_group(proc, signal.SIGKILL)
                raw_out, raw_err = await comm_task
            elif abort_task is not None and abort_task in done and comm_task not in done:
                cancelled = True
                _signal_group(proc, signal.SIGTERM)
                try:
                    raw_out, raw_err = await asyncio.wait_for(comm_task, timeout=_GRACE_S)
                except asyncio.TimeoutError:
                    _signal_group(proc, signal.SIGKILL)
                    raw_out, raw_err = await comm_task
            else:
                raw_out, raw_err = comm_task.result()
        except BaseException:
            _signal_group(proc, signal.SIGKILL)
            if not comm_task.done():
                comm_task.cancel()
            await asyncio.gather(comm_task, return_exceptions=True)
            raise
        finally:
            self._active.pop(exec_id, None)
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()
                await asyncio.gather(abort_task, return_exceptions=True)

        duration_ms = (time.monotonic() - t0) * 1000.0
        stdout, out_trunc = _trim(raw_out, spec.max_output_chars)
        stderr, err_trunc = _trim(raw_err, spec.max_output_chars)
        exit_code = proc.returncode if proc.returncode is not None else -9
        outcome = ProcessOutcome(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=out_trunc or err_trunc,
            duration_ms=duration_ms,
        )
        self._recorder.record_exec(spec, outcome, env_version=self.component_version())
        if timed_out or cancelled or exit_code != 0:
            _DIAG.anomaly(
                "exec.abnormal",
                exec_id=exec_id,
                exit_code=exit_code,
                timed_out=timed_out,
                cancelled=cancelled,
                duration_ms=duration_ms,
            )
        else:
            _DIAG.perf("exec.done", latency_ms=duration_ms, slow_ms=self._slow_ms, exec_id=exec_id, exit_code=exit_code)
        return outcome
