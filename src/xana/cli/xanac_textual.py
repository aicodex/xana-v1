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
    Horizontal { height: auto; }
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
        daemon_client: Any = None,
        session_id: str = "default",
        adapter_id: str = "cli",
        gateway: Any = None,   # legacy fallback (tests)
    ) -> None:
        super().__init__()
        self.daemon_client = daemon_client
        self.gateway = gateway
        self.session_id = session_id
        self.adapter_id = adapter_id
        self._fg_run_id: str | None = None
        self._tabs_widget: ListView | None = None
        self._frozen_runs: set[str] = set()
        self._state = UnifiedCardState(card_id=session_id, ph_run_id=session_id)
        self._sse_task: asyncio.Task | None = None

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

    def update_card(self, run_id: str, state: UnifiedCardState) -> None:
        """Replace fg-pane children with the latest widget tree."""
        try:
            pane = self.query_one("#fg-pane", VerticalScroll)
        except Exception:
            return
        pane.remove_children()
        for w in render_widgets(state):
            pane.mount(w)

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        self.query_one("#input", Input).value = ""

        if self.daemon_client is not None:
            asyncio.create_task(
                self.daemon_client.submit(self.session_id, text)
            )
            return

        # Legacy gateway path (kept for any remaining in-process tests)
        from xana.agent_adapter.contracts import InboundMessage
        inbound = InboundMessage(
            adapter_id=self.adapter_id,
            session_id=self.session_id,
            text=text,
        )

        async def _drain() -> None:
            try:
                async for _ in self.gateway.submit(inbound):
                    pass
            except Exception:
                log.exception("xanac submit drain crashed")

        asyncio.create_task(_drain())

    async def on_button_pressed(self, event) -> None:  # type: ignore[no-untyped-def]
        btn = event.button
        spec = getattr(btn, "spec", None)
        if spec is None or self.daemon_client is None:
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
            if self.daemon_client is not None:
                await self.daemon_client.attach(self.session_id, item.run_id)
            elif hasattr(self.gateway, "attach"):
                await self.gateway.attach(self.session_id, item.run_id)
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
