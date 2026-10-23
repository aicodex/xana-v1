"""进化迭代数据合约（计划 §6)——纯数据(dataclass/Literal/常量)。

- LedgerEntry(§6.1):append-only 账本条目,verdict ∈ {kept, rolled_back, abandoned}
- EvalCase/EvalSet(§6.2):评估集两账(visible 迭代用 / held-out 选择用)
- LintViolation(§6.1):账本 lint 违例

EpisodeGateError 是类型化错误(Exception 非 dataclass),按 checker 规则
不能留在 contracts.py,定义在 episode.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EpisodeVerdict = Literal["kept", "rolled_back", "abandoned"]
EpisodeState = Literal["declared", "running", "landing", "closing", "closed"]
EvalGroup = Literal["visible", "held_out"]
ChangeVerdict = Literal["pending", "accepted", "rejected"]

# closing 两阶段的落地动作类型(landing 期拒绝新动作的理由)
GATE_REASONS = ("episode_landing", "episode_closing", "episode_closed")


@dataclass(frozen=True)
class LedgerEntry:
    """账本单条记录(§6.1)。verdict 必填——append 时拒收 None。"""

    episode_id: str
    goal: str
    started_at: float
    budget: dict = field(default_factory=dict)
    forked_from: str | None = None
    branches: tuple[str, ...] = ()
    verdict: EpisodeVerdict | None = None
    delta_vs_base: float | None = None
    closure_tokens_used: int = 0
    continuation: tuple[str, ...] = ()
    # 迭代记录(iteration-agent-design §9.5):被迭代的 agent、假设、改动、归因、证据(批次与指标)
    agent: str = ""
    hypothesis: str = ""
    change: str = ""
    attribution: str = ""
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> "dict[str, Any]":
        return {
            "episode_id": self.episode_id,
            "goal": self.goal,
            "started_at": self.started_at,
            "budget": dict(self.budget),
            "forked_from": self.forked_from,
            "branches": list(self.branches),
            "verdict": self.verdict,
            "delta_vs_base": self.delta_vs_base,
            "closure_tokens_used": self.closure_tokens_used,
            "continuation": list(self.continuation),
            "agent": self.agent,
            "hypothesis": self.hypothesis,
            "change": self.change,
            "attribution": self.attribution,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "LedgerEntry":
        return cls(
            episode_id=str(raw.get("episode_id") or ""),
            goal=str(raw.get("goal") or ""),
            started_at=float(raw.get("started_at") or 0.0),
            budget=dict(raw.get("budget") or {}),
            forked_from=raw.get("forked_from"),
            branches=tuple(raw.get("branches") or ()),
            verdict=raw.get("verdict"),
            delta_vs_base=raw.get("delta_vs_base"),
            closure_tokens_used=int(raw.get("closure_tokens_used") or 0),
            continuation=tuple(raw.get("continuation") or ()),
            agent=str(raw.get("agent") or ""),
            hypothesis=str(raw.get("hypothesis") or ""),
            change=str(raw.get("change") or ""),
            attribution=str(raw.get("attribution") or ""),
            evidence=dict(raw.get("evidence") or {}),
        )


@dataclass(frozen=True)
class LintViolation:
    """账本 lint 违例(§6.1):条目缺 verdict / 行损坏。"""

    line_no: int
    reason: str


@dataclass(frozen=True)
class EvalCase:
    """评估集单条(§6.2)。group=visible 的分数可回流给元 agent;held-out 不回流。"""

    case_id: str
    prompt: str
    group: EvalGroup = "visible"


@dataclass(frozen=True)
class EvalSet:
    cases: tuple[EvalCase, ...] = ()

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "EvalSet":
        cases = tuple(
            EvalCase(
                case_id=str(c.get("case_id") or f"case-{i}"),
                prompt=str(c.get("prompt") or ""),
                group="held_out" if c.get("group") == "held_out" else "visible",
            )
            for i, c in enumerate(raw.get("cases") or ())
        )
        return cls(cases=cases)

    def visible(self) -> tuple[EvalCase, ...]:
        return tuple(c for c in self.cases if c.group == "visible")

    def held_out(self) -> tuple[EvalCase, ...]:
        return tuple(c for c in self.cases if c.group == "held_out")
