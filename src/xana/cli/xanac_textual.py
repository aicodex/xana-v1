"""xanac Textual REPL: interactive TUI driven by daemon SSE.

XanacApp consumes the daemon's HTTP+SSE stream via DaemonClient and renders
each `UnifiedCardState` snapshot into a Textual widget tree
(`render_widgets`). Input goes back through DaemonClient.submit, and card
buttons map to DaemonClient.button / detach as appropriate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from xana.agent_adapter.channels.unified_card import UnifiedCardState
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agent_adapter.channels.unified_card_cli import render_widgets


log = logging.getLogger(__name__)


class _TabItem(ListItem):
    def __init__(self, run_id: str, label: str) -> None:
        super().__init__(Label(label))
        self.run_id = run_id


def _evt_from_wire(data: dict):
    """Mirror of xanac._evt_from_wire — reconstruct AgentEvent from SSE."""
    from xana.agents.contracts import AgentEvent
    from xana.harness_sdk.contracts import HarnessEvent
    he = data.get("harness_event")
    harness_event = None
    if he:
        harness_event = HarnessEvent(
            kind=he["kind"], session_id=he["session_id"],
            run_id=he["run_id"], payload=he.get("payload") or {},
        )
    return AgentEvent(
        kind=data["kind"], session_id=data["session_id"],
        payload=data.get("payload") or {}, harness_event=harness_event,
    )


class XanacApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    #fg-pane { width: 3fr; border: solid green; padding: 1 2; }
    #tab-pane { width: 1fr; border: solid grey; }
    Input { dock: bottom; }
    .card-btn-row { height: auto; }
    """

    BINDINGS = [
        Binding("tab", "focus_tabs", "Tabs"),
        Binding("escape", "focus_input", "Input"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        daemon_client: Any,
        session_id: str = "default",
        adapter_id: str = "cli",
    ) -> None:
        super().__init__()
        self.daemon_client = daemon_client
        self.session_id = session_id
        self.adapter_id = adapter_id
        self._fg_run_id: str | None = None
        self._tabs_widget: ListView | None = None
        self._frozen_runs: set[str] = set()
        self._state = UnifiedCardState(card_id=session_id, ph_run_id=session_id)
        self._sse_task: asyncio.Task | None = None
        # Per-run content hash → skip mount when state's visible topology
        # + content lengths haven't changed. Prevents SSE-event flicker.
        self._last_hash: dict[str, tuple] = {}
        # Throttle handle for streaming text deltas (model_delta etc.).
        self._pending_flush: asyncio.TimerHandle | None = None
        self._pending_run_id: str | None = None
        self._FLUSH_DELAY = 0.05  # 50ms — coalesce SSE bursts

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield VerticalScroll(id="fg-pane")
            yield ListView(id="tab-pane")
        yield Input(placeholder="message…  (Tab: switch run, Esc: input)", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self._tabs_widget = self.query_one("#tab-pane", ListView)
        self.set_focus(self.query_one("#input", Input))
        if self.daemon_client is not None:
            self._sse_task = asyncio.create_task(self._consume_sse())

    async def on_unmount(self) -> None:
        if self._pending_flush is not None:
            self._pending_flush.cancel()
            self._pending_flush = None
        if self._sse_task is not None:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _consume_sse(self) -> None:
        sid = self.session_id
        try:
            async for ev in self.daemon_client.stream(sid):
                if ev.event == "initialize" or ev.event == "session.status":
                    continue
                if ev.event == "replay_gap":
                    self._state = UnifiedCardState(
                        card_id=sid, ph_run_id=sid)
                    self.update_card(sid, self._state)
                    continue
                if ev.event == "progress":
                    # detach / ph_replaced / compact_* / command_progress
                    # arrive as ProgressEvent via gateway.note_progress.
                    # Without this branch, /compact cards don't freeze and
                    # bg cards never get the terminal patch.
                    from xana.cli.xanac import _progress_from_wire
                    apply_event(self._state, _progress_from_wire(ev.data))
                    self._fg_run_id = sid
                    self.update_card(sid, self._state)
                    continue
                if ev.event != "agent_event":
                    continue
                aev = _evt_from_wire(ev.data)
                he = aev.harness_event
                if (he is not None and he.kind in ("final", "error")
                        and self._state.answer is None):
                    from xana.agent_adapter.channels.unified_card import (
                        AnswerSection,
                    )
                    self._state.answer = AnswerSection()
                apply_event(self._state, aev)
                self._fg_run_id = sid
                self.update_card(sid, self._state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("xanac SSE consume crashed")

    def _state_hash(self, state: UnifiedCardState) -> tuple:
        """Coarse content fingerprint — only rebuild when this changes.

        Captures section topology (presence/count) + content lengths +
        terminal states + button-row counts + footer text. Streaming
        deltas (model_delta etc.) bump the length tuple so the rebuild
        still fires, but identical-state events become no-ops.

        NOTE: Phase 1 settled for hash-based skip rather than full
        in-place mutation (kept as a TODO for Phase 3 follow-up).
        """
        c = state.compact
        c_tup = None if c is None else (
            c.state, c.compacted_n, c.progress_cells, len(c.summary_text or ""),
        )
        a = state.answer
        if a is None:
            a_tup = None
        else:
            tools_tup = tuple(
                (t.header, len(t.body_md or "")) for t in a.tools
            )
            a_tup = (
                a.state, len(a.thinking_text or ""),
                len(a.answer_text or ""), a.tools_pending, tools_tup,
            )
        menu_tup = None if state.menu is None else len(state.menu.body_md or "")
        top_tup = tuple(
            tuple((b.text, b.action, b.action_arg, b.send) for b in r.buttons)
            for r in state.top_buttons
        )
        bot_tup = tuple(
            tuple((b.text, b.action, b.action_arg, b.send) for b in r.buttons)
            for r in state.bottom_buttons
        )
        return (c_tup, a_tup, menu_tup, top_tup, bot_tup, state.footer.render())

    def update_card(self, run_id: str, state: UnifiedCardState) -> None:
        """Schedule a coalesced rebuild of fg-pane.

        Earlier the daemon-SSE path called remove_children + mount on
        EVERY wire event (model_delta, progress, …) → constant relayout
        flicker. We now:
          1) skip entirely if the coarse content hash hasn't changed
             (covers no-op progress / replay events);
          2) coalesce streaming text deltas into a single rebuild via
             a 50ms throttle (asyncio loop call_later).

        Phase 1 settled for hash-skip + throttle rather than full
        in-place widget mutation — left as a TODO for Phase 3.
        """
        h = self._state_hash(state)
        if self._last_hash.get(run_id) == h:
            return
        self._last_hash[run_id] = h
        self._pending_run_id = run_id
        if self._pending_flush is not None:
            return
        loop = asyncio.get_event_loop()
        self._pending_flush = loop.call_later(
            self._FLUSH_DELAY, self._flush_pending,
        )

    def _flush_pending(self) -> None:
        self._pending_flush = None
        run_id = self._pending_run_id
        if run_id is None:
            return
        self._pending_run_id = None
        try:
            pane = self.query_one("#fg-pane", VerticalScroll)
        except Exception:
            return
        pane.remove_children()
        for w in render_widgets(self._state):
            pane.mount(w)

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        self.query_one("#input", Input).value = ""
        asyncio.create_task(self.daemon_client.submit(self.session_id, text))

    async def on_button_pressed(self, event) -> None:  # type: ignore[no-untyped-def]
        btn = event.button
        spec = getattr(btn, "spec", None)
        if spec is None:
            return
        sid = self.session_id
        if spec.send:
            asyncio.create_task(
                self.daemon_client.submit(sid, spec.send)
            )
            return
        action = spec.action or ""
        if action in ("load_tools", "expand"):
            asyncio.create_task(self.daemon_client.button(sid, {
                "action": action,
                "action_arg": spec.action_arg or "",
                "ph_run_id": getattr(btn, "ph_run_id", "") or "",
                "card_id": getattr(btn, "card_id", "") or "",
            }))
            return
        # Default: treat as detach (cancel attention to a bg run)
        ph_run_id = getattr(btn, "ph_run_id", "") or ""
        asyncio.create_task(self.daemon_client.detach(sid, ph_run_id))

    def action_focus_tabs(self) -> None:
        if self._tabs_widget is not None:
            self.set_focus(self._tabs_widget)

    def action_focus_input(self) -> None:
        self.set_focus(self.query_one("#input", Input))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _TabItem):
            await self.daemon_client.attach(self.session_id, item.run_id)
            self.set_focus(self.query_one("#input", Input))


async def run_textual_with_daemon(*, session_id: str) -> int:
    from xana.cli.xanac import _ensure_serve_running, _resolve_endpoint
    from xana.cli._daemon_client import DaemonClient

    _ensure_serve_running()
    base, token, unix = _resolve_endpoint()
    client = DaemonClient(base_url=base, token=token, unix_socket=unix)
    try:
        app = XanacApp(daemon_client=client, session_id=session_id)
        await app.run_async()
    finally:
        await client.close()
    return 0
