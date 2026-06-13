"""TextualRenderer: drives a XanacApp instance from CliAdapter callbacks.

Buffers per-run text and dispatches widget updates on the attached
XanacApp. The app is wired up after construction with .attach(app)
since both objects need each other at boot.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xana.agent_adapter.interfaces import CliRenderer
from xana.agents.contracts import AgentEvent

if TYPE_CHECKING:
    from xana.agent_adapter.channels._draft import TurnDraft


class TextualRenderer(CliRenderer):
    """CliRenderer implementation backed by a Textual XanacApp.

    Calls before .attach(app) are no-ops on the UI side; per-run text
    buffers are still maintained so a late attach can replay the body.
    """

    def __init__(self) -> None:
        self.app: Any | None = None
        self._buffers: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._fg: str | None = None
        self._bg: list[str] = []  # ordered bg run_ids

    def attach(self, app: Any) -> None:
        self.app = app
        # Best-effort replay: paint current foreground (if any) into the app.
        if self._fg is not None:
            app.show_run(
                self._fg,
                label=self._labels.get(self._fg, ""),
                body=self._buffers.get(self._fg, ""),
            )
            app.refresh_tabs(list(self._bg), labels=dict(self._labels))

    async def on_run_started(self, run_id: str, label: str) -> None:
        self._labels[run_id] = label
        self._buffers.setdefault(run_id, "")
        if self._fg is not None and self._fg != run_id and self._fg not in self._bg:
            self._bg.append(self._fg)
        if run_id in self._bg:
            self._bg.remove(run_id)
        self._fg = run_id
        if self.app is not None:
            self.app.show_run(run_id, label=label, body="")
            self.app.refresh_tabs(list(self._bg), labels=dict(self._labels))

    async def on_event(self, run_id: str, event: AgentEvent) -> None:
        if event.kind != "draft":
            return
        payload = event.payload or {}
        text = payload.get("text") or payload.get("delta") or ""
        if not text:
            return
        self._buffers[run_id] = self._buffers.get(run_id, "") + str(text)
        if self.app is not None and run_id == self._fg:
            self.app.update_run_body(run_id, self._buffers[run_id])

    async def on_run_detached(self, run_id: str) -> None:
        if run_id == self._fg:
            self._fg = None
        if run_id not in self._bg:
            self._bg.append(run_id)
        if self.app is not None:
            self.app.freeze_run(run_id, label=self._labels.get(run_id, ""))
            self.app.refresh_tabs(list(self._bg), labels=dict(self._labels))

    async def on_foreground_changed(self, run_id: str) -> None:
        if (
            self._fg is not None
            and self._fg != run_id
            and self._fg not in self._bg
        ):
            self._bg.append(self._fg)
        if run_id in self._bg:
            self._bg.remove(run_id)
        self._fg = run_id
        if self.app is not None:
            self.app.show_run(
                run_id,
                label=self._labels.get(run_id, ""),
                body=self._buffers.get(run_id, ""),
            )
            self.app.refresh_tabs(list(self._bg), labels=dict(self._labels))

    async def on_ph_replaced(
        self, run_id: str, draft: "TurnDraft | None"
    ) -> None:
        body = self._buffers.get(run_id, "")
        if draft is not None and hasattr(draft, "content"):
            body = getattr(draft, "content") or body
        if self.app is not None:
            self.app.finalize_run(
                run_id,
                body=body,
                label=self._labels.get(run_id, ""),
            )

    async def on_run_completed_bg(
        self, run_id: str, draft: "TurnDraft | None"
    ) -> None:
        body = self._buffers.get(run_id, "")
        if draft is not None and hasattr(draft, "content"):
            body = getattr(draft, "content") or body
        if self.app is not None:
            self.app.finalize_run(
                run_id, body=body, label=self._labels.get(run_id, "")
            )

    async def on_compact_started(self, run_id: str, fork_point: int) -> None:
        if self.app is not None:
            self.app.show_run(run_id, label="compact", body="")

    async def on_compact_finalized(self, run_id: str, summary: str) -> None:
        if self.app is not None:
            self.app.finalize_run(
                run_id, body=summary or "(empty summary)", label="compact"
            )
