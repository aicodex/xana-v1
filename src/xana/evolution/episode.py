"""EpisodeRunner —— 迭代 episode 状态机(计划 §6.2)。

状态:declared → running → landing → closing → closed,载体是
RunRecord.budget + metadata["episode"](§6.2: harness 升级,不是新 loop)。

- 收尾预留:closure_estimate = 账本历史 closure_tokens_used 的 P75
  (无历史 → DEFAULT_CLOSURE_TOKENS);软阈值 = budget − closure_estimate,
  触发 landing:spawn/分叉/新评估被拒(EpisodeGateError 类型化),
  在途评估等待完成。
- closing 两阶段:① canary 回归({home}/canary/*.txt 固定小任务集,
  验收"迭代后的 Agent 没坏") ② 增益判决(Δ vs base,评估集 visible /
  held-out 两账;held-out 分数不回流给元 prompt,见 render_eval_summary)。
- 硬阈值:累计 token 超 budget.max_tokens → 强制 closing。
- 开启新 episode(declare)前扫描僵尸 episode(closed 之外)→ 强制闭合
  为 abandoned 并落账本。
- 文档冻结:declare 时快照 doc_path 内容;close 时 verdict != kept →
  回滚快照(回滚是 harness 侧写,不经过模型工具调用)。

仅 concrete helper(boot 组装,不抽 ABC)。
"""
from __future__ import annotations

import json
import math
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from xana.evolution.contracts import (
    EpisodeState,
    EvalCase,
    EvalSet,
    LedgerEntry,
)


class EpisodeGateError(Exception):
    """landing/closing/closed 期发起 spawn/分叉/新评估被拒(§6.2,类型化)。"""

    def __init__(self, reason: str, *, action: str = "", state: str = "") -> None:
        self.reason = reason  # GATE_REASONS 之一
        self.action = action
        self.state = state
        super().__init__(
            f"{reason}: episode 处于 {state} 状态,拒绝 {action or '该动作'}"
        )
from xana.evolution.ledger import Ledger, default_evolution_home
from xana.state.contracts import RunRecord
from xana.state.interfaces import RunStore

DEFAULT_CLOSURE_TOKENS = 20_000
DEFAULT_CANARY_PROMPTS = (
    "用一句话说明你的目标。",
    "回复 OK。",
)
EPISODE_SESSION_ID = "evolution"

CanaryFn = Callable[[str], Awaitable[bool]]


def _percentile_75(values: list[int]) -> int:
    """nearest-rank P75。调用方保证 values 非空。"""
    ordered = sorted(values)
    idx = max(0, math.ceil(0.75 * len(ordered)) - 1)
    return ordered[idx]


def load_canary_prompts(home: Path) -> tuple[str, ...]:
    """{home}/canary/*.txt,一文件一 prompt;缺省用内置最小任务集。"""
    canary_dir = Path(home) / "canary"
    if canary_dir.is_dir():
        prompts = tuple(
            p.read_text(encoding="utf-8").strip()
            for p in sorted(canary_dir.glob("*.txt"))
            if p.read_text(encoding="utf-8").strip()
        )
        if prompts:
            return prompts
    return DEFAULT_CANARY_PROMPTS


def load_eval_set(home: Path) -> EvalSet:
    """{home}/evalset.json → 评估集(visible/held-out 两账)。"""
    path = Path(home) / "evalset.json"
    if not path.exists():
        return EvalSet()
    try:
        return EvalSet.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return EvalSet()


def render_eval_summary(
    eval_set: EvalSet, scores: dict[str, float], *, include_held_out: bool = False,
) -> str:
    """评估结果摘要。元 prompt 注入用可见集;held-out 分数不回流(§6.2)。"""
    cases: tuple[EvalCase, ...] = eval_set.cases if include_held_out else eval_set.visible()
    lines = []
    for case in cases:
        mark = "held-out" if case.group == "held_out" else "visible"
        lines.append(f"  - {case.case_id} ({mark}): {scores.get(case.case_id, 'n/a')}")
    return "\n".join(lines) if lines else "  (无评估记录)"


class EpisodeRunner:
    """单个活跃 episode 的状态机;episode 记录持久化在 RunStore。"""

    def __init__(
        self,
        *,
        ledger: Ledger,
        run_store: RunStore | None = None,
        home: Path | None = None,
        clock: Callable[[], float] = _time.time,
        default_closure_tokens: int = DEFAULT_CLOSURE_TOKENS,
    ) -> None:
        self._ledger = ledger
        self._run_store = run_store
        self._home = Path(home) if home is not None else default_evolution_home()
        self._clock = clock
        self._default_closure = default_closure_tokens
        # 当前激活的 episode(in-memory 镜像;权威在 RunRecord.metadata["episode"])
        self._current: dict[str, Any] | None = None
        self._doc_snapshot: str | None = None
        self._doc_path: Path | None = None
        self._tokens_used = 0

    # ------------------------------------------------------------------
    # declare / 僵尸闭合
    # ------------------------------------------------------------------

    async def declare(
        self,
        *,
        episode_id: str,
        goal: str,
        budget: dict,
        forked_from: str | None = None,
        doc_path: Path | None = None,
        session_id: str = EPISODE_SESSION_ID,
        run_id: str | None = None,
    ) -> str:
        """开启新 episode。先扫描僵尸 episode(非 closed)→ 强制闭合 abandoned。"""
        # 全新迭代(无 forked_from):旧 episode 是僵尸 → 强制闭合。
        # 分叉(forked_from=当前 episode):保留父 lineage。
        exclude = {episode_id}
        if forked_from:
            exclude.add(forked_from)
        if self._current is not None and not forked_from:
            exclude.discard(self._ep()["episode_id"])
        await self._force_close_zombies(exclude_ids=exclude)

        rid = run_id or f"episode-{episode_id}"
        md: dict[str, Any] = {
            "episode": {
                "episode_id": episode_id,
                "state": "declared",
                "goal": goal,
                "forked_from": forked_from,
                "branches": [],
                "started_at": self._clock(),
            },
        }
        rec = RunRecord(
            run_id=rid, session_id=session_id, agent_name="meta",
            status="running", budget=dict(budget), metadata=md,
        )
        if self._run_store is not None:
            try:
                await self._run_store.create(rec)
            except FileExistsError:
                await self._run_store.update_status(rid, "running", fields=md)
        self._activate(rec)
        self._doc_path = Path(doc_path) if doc_path is not None else None
        self._doc_snapshot = (
            self._doc_path.read_text(encoding="utf-8")
            if self._doc_path is not None and self._doc_path.exists()
            else None
        )
        self._tokens_used = 0
        return rid

    async def start(self) -> None:
        self._require_current()
        await self._transition("running")

    # ------------------------------------------------------------------
    # 状态 / 阈值
    # ------------------------------------------------------------------

    def state(self) -> EpisodeState | None:
        if self._current is None:
            return None
        return self._ep()["state"]

    def current_episode_id(self) -> str | None:
        if self._current is None:
            return None
        return self._ep()["episode_id"]

    async def state_record(self, episode_id: str) -> RunRecord:
        """按 episode_id 找记录(当前内存镜像或 RunStore)。"""
        if (
            self._current is not None
            and self._ep()["episode_id"] == episode_id
        ):
            return self._current
        if self._run_store is not None:
            for rec in await self._run_store.list_all():
                ep = rec.metadata.get("episode")
                if isinstance(ep, dict) and ep.get("episode_id") == episode_id:
                    return rec
        raise LookupError(f"unknown episode {episode_id!r}")

    async def activate(self, run_id: str) -> None:
        """把内存当前指针切回指定 run_id 的 episode 记录(测试/多 episode 场景)。"""
        if self._run_store is None:
            raise LookupError(run_id)
        rec = await self._run_store.get(run_id)
        if rec is None:
            raise LookupError(run_id)
        self._activate(rec)

    def soft_threshold(self) -> int | None:
        """budget.max_tokens − closure_estimate;无上限返回 None。"""
        if self._current is None:
            return None
        max_tokens = self._current.budget.get("max_tokens")
        if not isinstance(max_tokens, (int, float)):
            return None
        return int(max_tokens) - self.closure_estimate()

    async def record_usage(self, tokens: int) -> str | None:
        """harness 每轮调用。软阈值 → landing(返回 None);硬阈值 → closing
        (返回 "episode_hard_threshold")。其它状态返回 None。"""
        if self._current is None:
            return None
        state = self.state()
        if state not in ("running", "landing"):
            return None
        self._tokens_used += max(0, int(tokens))
        max_tokens = self._current.budget.get("max_tokens")
        if not isinstance(max_tokens, (int, float)):
            return None
        if self._tokens_used >= int(max_tokens):
            await self._transition("closing")
            return "episode_hard_threshold"
        if state == "running":
            soft = self.soft_threshold()
            if soft is not None and self._tokens_used >= soft:
                await self._transition("landing")
        return None

    # ------------------------------------------------------------------
    # landing 门禁
    # ------------------------------------------------------------------

    def ensure_gate(self, action: str) -> None:
        """landing/closing/closed 期拒绝 spawn/分叉/新评估(类型化错误)。"""
        state = self.state()
        if state in ("landing", "closing", "closed"):
            raise EpisodeGateError(
                f"episode_{state}", action=action, state=state or "?",
            )

    def note_branch(self, run_id: str) -> None:
        """记录本 episode 分叉出的子 run(写入 metadata.branches)。"""
        if self._current is None:
            return
        branches = list(self._ep().get("branches") or [])
        branches.append(run_id)
        self._ep()["branches"] = branches

    # ------------------------------------------------------------------
    # closing 两阶段 + 账本闭合
    # ------------------------------------------------------------------

    async def close(
        self,
        *,
        canary_fn: CanaryFn | None = None,
        eval_base: dict[str, float] | None = None,
        eval_scores: dict[str, float] | None = None,
        tokens_used: int | None = None,
    ) -> LedgerEntry:
        """closing 两阶段:① canary 回归 ② 增益判决(Δ vs base)。
        落账本 + 文档回滚(kept 除外),状态 → closed。"""
        self._require_current()
        ep = self._ep()
        if ep["state"] == "closed":
            raise RuntimeError(f"episode {ep['episode_id']} 已闭合")
        # 闭合前先清僵尸(防止分叉子 episode 悬挂)
        await self._force_close_zombies(exclude_ids={ep["episode_id"]})
        await self._transition("closing")

        # ① canary:固定小任务集,任一失败 → 迭代后的 Agent 坏了
        canary_ok = True
        if canary_fn is not None:
            import asyncio as _asyncio

            for prompt in load_canary_prompts(self._home):
                result = canary_fn(prompt)
                ok = await result if _asyncio.iscoroutine(result) else bool(result)
                if not ok:
                    canary_ok = False
                    break

        # ② 增益判决:Δ vs base(评估集两账,调用方负责 held-out 不回流)
        delta: float | None = None
        if eval_scores:
            pairs = [
                (eval_scores[cid], base)
                for cid, base in (eval_base or {}).items()
                if cid in eval_scores
            ]
            if pairs:
                new_mean = sum(s for s, _ in pairs) / len(pairs)
                base_mean = sum(b for _, b in pairs) / len(pairs)
                delta = new_mean - base_mean

        if not canary_ok or (delta is not None and delta <= 0):
            verdict: str = "rolled_back"
        else:
            verdict = "kept"

        if verdict != "kept" and self._doc_path is not None and self._doc_snapshot is not None:
            # 回滚是 harness 侧写,不经过模型工具调用门禁。
            self._doc_path.parent.mkdir(parents=True, exist_ok=True)
            self._doc_path.write_text(self._doc_snapshot, encoding="utf-8")

        tokens = self._tokens_used if tokens_used is None else int(tokens_used)
        entry = LedgerEntry(
            episode_id=ep["episode_id"],
            goal=ep["goal"],
            started_at=float(ep.get("started_at") or 0.0),
            budget=dict(self._current.budget),
            forked_from=ep.get("forked_from"),
            branches=tuple(ep.get("branches") or ()),
            verdict=verdict,  # type: ignore[arg-type]
            delta_vs_base=delta,
            closure_tokens_used=tokens,
            continuation=tuple(await self._continuation_ids(ep["episode_id"])),
        )
        await self._ledger.append(entry)
        await self._transition("closed")
        if eval_scores:
            # 上轮评估结果落盘:元 prompt 只回流 visible 集(§6.2 两账纪律)。
            try:
                self._home.mkdir(parents=True, exist_ok=True)
                (self._home / "last_eval.json").write_text(json.dumps(
                    {"scores": dict(eval_scores), "delta_vs_base": delta},
                    ensure_ascii=False,
                ), encoding="utf-8")
            except OSError:
                pass
        return entry

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _activate(self, rec: RunRecord) -> None:
        self._current = rec

    def _require_current(self) -> None:
        if self._current is None:
            raise RuntimeError("没有进行中的 episode(先 declare)")

    def _ep(self):
        self._require_current()
        return self._current.metadata["episode"]

    async def _transition(self, state: EpisodeState) -> None:
        self._require_current()
        md = dict(self._current.metadata)
        ep = dict(md["episode"])
        ep["state"] = state
        md["episode"] = ep
        self._current = replace(self._current, metadata=md)
        if self._run_store is not None:
            await self._run_store.update_status(
                self._current.run_id, self._current.status, fields={"metadata": md},
            )

    def closure_estimate(self) -> int:
        """账本历史 closure_tokens_used 的 P75;无历史用默认常量。

        同步直读小 JSONL(调用方有同步场景,如 soft_threshold)。"""
        entries = [
            e.closure_tokens_used
            for e in _read_entries_sync(self._ledger)
            if e.closure_tokens_used > 0
        ]
        if not entries:
            return self._default_closure
        return _percentile_75(entries)

    async def _force_close_zombies(self, *, exclude_ids: set[str] | None = None) -> None:
        """扫描 RunStore 里 state != closed 的 episode → 强制闭合 abandoned 落账本。
        exclude_ids:不碰的 episode(自身 / 分叉父 lineage)。"""
        if self._run_store is None:
            return
        skip = exclude_ids or set()
        for rec in await self._run_store.list_all():
            ep = rec.metadata.get("episode")
            if not isinstance(ep, dict):
                continue
            if ep.get("state") == "closed":
                continue
            if ep.get("episode_id") in skip:
                continue
            md = dict(rec.metadata)
            closed = dict(ep)
            closed["state"] = "closed"
            md["episode"] = closed
            await self._run_store.update_status(rec.run_id, rec.status, fields={"metadata": md})
            usage = rec.usage
            tokens = (
                (usage.exact_input_tokens or usage.estimated_input_tokens or 0)
                + (usage.exact_output_tokens or usage.estimated_output_tokens or 0)
            )
            await self._ledger.append(LedgerEntry(
                episode_id=str(ep.get("episode_id") or rec.run_id),
                goal=str(ep.get("goal") or ""),
                started_at=float(ep.get("started_at") or 0.0),
                budget=dict(rec.budget),
                forked_from=ep.get("forked_from"),
                branches=tuple(ep.get("branches") or ()),
                verdict="abandoned",
                delta_vs_base=None,
                closure_tokens_used=int(tokens),
                continuation=(),
            ))

    async def _continuation_ids(self, episode_id: str) -> list[str]:
        """forked_from == episode_id 的子 episode(含僵尸闭合的)。"""
        out: list[str] = []
        if self._run_store is None:
            return out
        for rec in await self._run_store.list_all():
            ep = rec.metadata.get("episode")
            if isinstance(ep, dict) and ep.get("forked_from") == episode_id:
                out.append(str(ep.get("episode_id")))
        return out


def _read_entries_sync(ledger: Ledger) -> list[LedgerEntry]:
    """同步直读账本 JSONL(closure_estimate 有同步调用方;文件小,可接受)。"""
    path = ledger.path
    if not path.exists():
        return []
    return [
        LedgerEntry.from_json(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
