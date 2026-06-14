from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from xana.agent_adapter.channels._draft import TurnDraft


@dataclass
class RunRenderState:
    run_id: str
    label: str
    turns: "OrderedDict[str, TurnDraft]" = field(default_factory=OrderedDict)
    card_anchor: Any = None  # channel-specific msg id / widget id
    is_foreground: bool = False
    is_frozen: bool = True


@dataclass
class BgCardState:
    """Per-run background placeholder card state (dual-card model)."""
    message_id: str
    label: str
    state: str = "running"
    progress_pct: int = 0
    panel_md: str = ""
    compacted_n: int = 0


@dataclass
class SessionRenderState:
    """Adapter-side per-session render state: tracks runs, foreground, and provides PH-driven transitions."""

    runs: "OrderedDict[str, RunRenderState]" = field(default_factory=OrderedDict)
    foreground_run_id: str | None = None
    max_runs: int = 4

    # Dual-card model (Group E): single fg streaming card + lazy bg placeholder
    # cards per detached run.
    fg_run_id: str | None = None
    fg_message_id: str | None = None
    bg_cards: dict[str, BgCardState] = field(default_factory=dict)

    def start_run(self, run_id: str, label: str) -> RunRenderState:
        """Create a new run as foreground; demote previous foreground to frozen background."""
        if self.foreground_run_id and self.foreground_run_id in self.runs:
            self.runs[self.foreground_run_id].is_foreground = False
            self.runs[self.foreground_run_id].is_frozen = True
        st = RunRenderState(
            run_id=run_id, label=label, is_foreground=True, is_frozen=False
        )
        self.runs[run_id] = st
        self.foreground_run_id = run_id
        while len(self.runs) > self.max_runs:
            self.runs.popitem(last=False)
        return st

    def switch_foreground(self, run_id: str) -> None:
        """Make run_id the foreground (frozen=False); freeze previous foreground. No-op if unknown."""
        if run_id not in self.runs:
            return
        if self.foreground_run_id and self.foreground_run_id in self.runs:
            self.runs[self.foreground_run_id].is_foreground = False
            self.runs[self.foreground_run_id].is_frozen = True
        self.runs[run_id].is_foreground = True
        self.runs[run_id].is_frozen = False
        self.foreground_run_id = run_id

    def detach(self, run_id: str) -> None:
        """Mark run as frozen background; clear foreground_run_id if it was foreground."""
        if run_id in self.runs:
            self.runs[run_id].is_frozen = True
            self.runs[run_id].is_foreground = False
            if self.foreground_run_id == run_id:
                self.foreground_run_id = None

    def bg_runs(self) -> list[RunRenderState]:
        """Return non-foreground runs in insertion order."""
        return [r for r in self.runs.values() if not r.is_foreground]
