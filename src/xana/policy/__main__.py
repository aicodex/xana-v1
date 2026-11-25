"""Standalone CLI for xana.policy: `python -m xana.policy <bench|check>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from xana.policy.approvals import PermissionService
from xana.policy.contracts import AuthorizationRequest, PermissionContext, PermissionResult, ProcessSpec, SessionPolicySnapshot
from xana.policy.exec_env import LocalProcessEnvironment
from xana.policy.path_policy import ProtectedDirsPathPolicy
from xana.policy.shell import TreeSitterShellClassifier
from xana.policy.testing import InMemoryPolicyState
from xana.protocol.eval import EvalTrialRecord, MetricDefinition, RunSummary, build_benchmark_snapshot
from xana.protocol.versioning import to_wire


class _NeverAbort:
    aborted = False

    async def wait(self) -> None:
        await asyncio.Event().wait()


async def _cmd_bench() -> int:
    """Run deterministic security & execution-boundary benchmark suite and emit BenchmarkSnapshot."""
    clf = TreeSitterShellClassifier()
    trials: list[EvalTrialRecord] = []

    def _add(task_id: str, ok: bool, duration_ms: float) -> None:
        trials.append(EvalTrialRecord(
            trial_id=f"trial-{task_id}",
            benchmark="policy_security_bench",
            task_id=task_id,
            split="train",
            status="pass" if ok else "fail",
            score=1.0 if ok else 0.0,
            duration_ms=round(duration_ms, 2),
            run_summary=RunSummary(step_count=1),
        ))

    # 1. Dangerous shell detection
    t0 = time.monotonic()
    dangerous_cmds = ["rm -rf /", "sudo rm -rf /etc", "dd if=/dev/zero of=/dev/sda", "bash -c 'rm -rf /home'"]
    ok_d = all(clf.classify(c).risk == "dangerous" for c in dangerous_cmds)
    _add("dangerous_shell_block", ok_d, (time.monotonic() - t0) * 1000.0)

    # 2. Routine shell allow
    t0 = time.monotonic()
    routine_cmds = ["ls -la", "pytest -q", "rm -rf /tmp/test_dir", "git status"]
    ok_r = all(clf.classify(c).risk == "routine" for c in routine_cmds)
    _add("routine_shell_allow", ok_r, (time.monotonic() - t0) * 1000.0)

    # 3. Plan mode denies write tools
    t0 = time.monotonic()
    state = InMemoryPolicyState({"s1": SessionPolicySnapshot(session_id="s1", permission_mode="auto", plan_mode=True)})
    svc = PermissionService(state=state)
    res = await svc.authorize(
        AuthorizationRequest("s1", "r1", "c1", "Write", {"file_path": "a.py"}, "/tmp", False, PermissionContext("auto", "s1")),
        PermissionResult("allow"),
        _NeverAbort(),
    )
    _add("plan_mode_readonly_deny", res.decision == "deny", (time.monotonic() - t0) * 1000.0)

    # 4. Protected dir write deny
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        mem_dir = Path(td) / "memory"
        mem_dir.mkdir()
        path_pol = ProtectedDirsPathPolicy(protected_dirs=(mem_dir,))
        r_deny = path_pol.check_path(mem_dir / "note.md", operation="write", cwd=Path(td))
        r_allow = path_pol.check_path(Path(td) / "src.py", operation="write", cwd=Path(td))
        _add("protected_dir_boundary", r_deny.decision == "deny" and r_allow.decision == "allow", (time.monotonic() - t0) * 1000.0)

    # 5. Subprocess timeout & process-group cleanup
    t0 = time.monotonic()
    env = LocalProcessEnvironment()
    out = await env.run_process(ProcessSpec(cwd="/tmp", shell="sleep 30", timeout_s=0.15))
    _add("process_timeout_cleanup", out.timed_out and out.exit_code != 0, (time.monotonic() - t0) * 1000.0)

    snap = build_benchmark_snapshot(
        snapshot_id=f"policy-bench-{int(time.time())}",
        benchmark="policy_security_bench",
        created_at_ms=int(time.time() * 1000),
        git_sha="local",
        manifest_hash="",
        metric_definitions=(MetricDefinition(key="pass_rate", label="Pass Rate", format="percent"),),
        trials=tuple(trials),
    )
    print(json.dumps(to_wire(snap), ensure_ascii=False, indent=2))
    return 0 if snap.failed == 0 and snap.errored == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xana.policy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bench", help="run security and execution-boundary benchmark suite")
    p_chk = sub.add_parser("check", help="classify a shell command risk")
    p_chk.add_argument("command", type=str)
    args = parser.parse_args(argv)

    if args.cmd == "bench":
        return asyncio.run(_cmd_bench())
    if args.cmd == "check":
        res = TreeSitterShellClassifier().classify(args.command)
        print(json.dumps(to_wire(res), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
