"""启动扫 runs/ 目录,产 ScanReport — 给 boot 后续处理 PH 替换 + /resume 候选填充。

仲裁规则(P5.5):有 run_completed marker 则 A 赢,不管 JSONL 看起来是否完整。
.aborted.jsonl 后缀文件跳过(已是 resume fork 的源)。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanReport:
    completed_run_ids: tuple[str, ...] = ()    # 应触发 PH 替换
    unfinished_run_ids: tuple[str, ...] = ()   # 应入 /resume(exit_reason=shutdown_force)
    aborted_run_ids: tuple[str, ...] = ()      # 已是 .aborted.jsonl,跳过


def scan_runs_dir(runs_root: pathlib.Path) -> ScanReport:
    if not runs_root.exists():
        return ScanReport()
    completed: list[str] = []
    unfinished: list[str] = []
    aborted: list[str] = []
    for path in sorted(runs_root.iterdir()):
        name = path.name
        if name.endswith(".aborted.jsonl"):
            aborted.append(name[: -len(".aborted.jsonl")])
            continue
        if not name.endswith(".jsonl"):
            continue
        run_id = name[: -len(".jsonl")]
        has_completed_marker = False
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("marker") == "run_completed":
                        has_completed_marker = True
                        break
        except OSError:
            continue
        if has_completed_marker:
            completed.append(run_id)
        else:
            unfinished.append(run_id)
    return ScanReport(
        completed_run_ids=tuple(completed),
        unfinished_run_ids=tuple(unfinished),
        aborted_run_ids=tuple(aborted),
    )


async def replace_completed_placeholders(
    *,
    report: ScanReport,
    runs_root: pathlib.Path,
    session_id: str,
    agent_name: str,
    store,  # ActiveContextStore
) -> None:
    """对每个 completed run,从 jsonl 读出 messages 替换 PH。"""
    from xana.memory.runs_loader import runs_jsonl_to_memory_messages
    for rid in report.completed_run_ids:
        jsonl_path = runs_root / f"{rid}.jsonl"
        try:
            messages = runs_jsonl_to_memory_messages(jsonl_path)
        except Exception:
            continue
        # ActiveContextStore.replace_placeholder takes (run_id, replace_with)
        # only — session_id/agent_name are not forwarded; keep them in the
        # helper signature for boot-time call sites that still pass them.
        await store.replace_placeholder(rid, tuple(messages))


async def mark_unfinished_as_resumable(
    *,
    report: ScanReport,
    store,  # RunStore
    session_id: str,
    agent_name: str,
) -> None:
    """把扫到的未完成 run 写入 RunStore,exit_reason=shutdown_force,status=paused。

    已存在则 update_status 到 paused + exit_reason=shutdown_force。
    """
    from xana.state.contracts import RunRecord
    for rid in report.unfinished_run_ids:
        existing = await store.get(rid)
        if existing is None:
            await store.create(RunRecord(
                run_id=rid, session_id=session_id, agent_name=agent_name,
                status="paused", exit_reason="shutdown_force",
            ))
        else:
            await store.update_status(
                rid, "paused", fields={"exit_reason": "shutdown_force"},
            )
    # Orphan sweep: any RunRecord with status running/paused but no exit_reason
    # and no corresponding jsonl in runs/ (pre-jsonl crash or lost log) is also
    # an orphan. Mark them shutdown_force so list_unfinished surfaces them.
    list_all = getattr(store, "list_all", None)
    if list_all is None:
        return
    for rec in await list_all():
        if rec.exit_reason is not None:
            continue
        if rec.status not in ("running", "paused"):
            continue
        await store.update_status(
            rec.run_id, "paused", fields={"exit_reason": "shutdown_force"},
        )
