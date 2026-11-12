"""xanac Textual REPL: interactive TUI driven by daemon SSE.

XanacApp consumes the daemon's HTTP+SSE stream via DaemonClient and renders
each `UnifiedCardState` snapshot into a Textual widget tree
(`render_widgets`). Input goes back through DaemonClient.submit, and card
buttons map to DaemonClient.button / cancel as appropriate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input

from xana.agent_adapter.channels.daemon_bridge import agent_event_from_wire
from xana.agent_adapter.channels.unified_card import UnifiedCardState
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agent_adapter.channels.unified_card_cli import render_widgets


log = logging.getLogger(__name__)


class XanacApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    #fg-pane { width: 3fr; border: solid green; padding: 1 2; }
    Input { dock: bottom; }
    .card-btn-row { height: auto; }
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "Stop"),
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
        self._active_run_id: str | None = None
        self._states: dict[str, UnifiedCardState] = {}
        self._state = UnifiedCardState(card_id=session_id, run_id=session_id)
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
        yield Button("停止回答", id="stop-turn", disabled=True)
        yield Input(placeholder="message…  (Esc: stop)", id="input")
        yield Footer()

    def on_mount(self) -> None:
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
                    self._states.clear()
                    self._state = UnifiedCardState(card_id=sid)
                    self.update_card(sid, self._state)
                    continue
                if ev.event == "progress":
                    from xana.cli.xanac import _progress_from_wire
                    event = _progress_from_wire(ev.data)
                    rid = event.run_id
                elif ev.event == "agent_event":
                    event = agent_event_from_wire(ev.data)
                    rid = event.harness_event.run_id if event.harness_event else str(event.payload.get("run_id") or "")
                else:
                    continue
                if not rid:
                    continue
                fresh = rid not in self._states
                state = self._states.setdefault(rid, UnifiedCardState(card_id=rid, run_id=rid))
                apply_event(state, event)
                he = event.harness_event if ev.event == "agent_event" else None
                if he is not None and he.kind in {"turn_started", "input_accepted"}:
                    self._active_run_id = rid
                if rid == self._active_run_id:
                    self._fg_run_id = rid
                    if state.terminal:
                        self._active_run_id = None
                elif fresh:
                    self._fg_run_id = rid
                self.query_one("#stop-turn", Button).disabled = self._active_run_id is None
                if rid == self._fg_run_id:
                    self._state = state
                    self.update_card(rid, state)
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
        if btn.id == "stop-turn":
            await self.action_cancel_turn()
            return
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
        if action == "load_tools":
            from xana.agent_adapter.channels.unified_card import load_pending_tools
            state = self._states.get(btn.card_id)
            if state is not None and state.answer is not None:
                load_pending_tools(state.answer)
                self._state = state
                self.update_card(state.card_id, state)
            return
        if action == "expand":
            asyncio.create_task(self.daemon_client.button(sid, {
                "action": action,
                "action_arg": spec.action_arg or "",
                "run_id": getattr(btn, "run_id", "") or "",
                "card_id": getattr(btn, "card_id", "") or "",
            }))
            return
        if action == "compact_cancel":
            asyncio.create_task(self.daemon_client.submit(sid, f"/compact_cancel {spec.action_arg}"))
            return
        if action == "cancel":
            run_id = spec.action_arg or ""
            if run_id:
                asyncio.create_task(self.daemon_client.cancel(sid, run_id))

    async def action_cancel_turn(self) -> None:
        if self._active_run_id:
            await self.daemon_client.cancel(self.session_id, self._active_run_id)
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
