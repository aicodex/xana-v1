"""xanac Textual REPL: interactive TUI sharing the oneshot pipeline.

Minimal Textual app. Foreground pane shows the streaming body of the
current run; side ListView shows frozen/background runs (Tab to focus,
Enter to switch). Bottom Input submits to gateway. Renderer-facing API
(show_run/update_run_body/freeze_run/finalize_run/refresh_tabs) is
called by TextualRenderer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static


class _TabItem(ListItem):
    def __init__(self, run_id: str, label: str) -> None:
        super().__init__(Label(label))
        self.run_id = run_id


class XanacApp(App):
    CSS = """
    Horizontal { height: 1fr; }
    #fg-pane { width: 3fr; border: solid green; padding: 1 2; }
    #tab-pane { width: 1fr; border: solid grey; }
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("tab", "focus_tabs", "Tabs"),
        Binding("escape", "focus_input", "Input"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        gateway: Any,
        session_id: str,
        adapter_id: str = "cli",
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.session_id = session_id
        self.adapter_id = adapter_id
        self._fg_run_id: str | None = None
        self._tabs_widget: ListView | None = None
        self._frozen_runs: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield VerticalScroll(Static("", id="fg-body"), id="fg-pane")
            yield ListView(id="tab-pane")
        yield Input(placeholder="message…  (Tab: switch run, Esc: input)", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self._tabs_widget = self.query_one("#tab-pane", ListView)
        self.set_focus(self.query_one("#input", Input))

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        self.query_one("#input", Input).value = ""
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
                logging.getLogger(__name__).exception(
                    "xanac submit drain crashed"
                )

        asyncio.create_task(_drain())

    def action_focus_tabs(self) -> None:
        if self._tabs_widget is not None:
            self.set_focus(self._tabs_widget)

    def action_focus_input(self) -> None:
        self.set_focus(self.query_one("#input", Input))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _TabItem) and hasattr(self.gateway, "attach"):
            await self.gateway.attach(self.session_id, item.run_id)
            self.set_focus(self.query_one("#input", Input))

    # ----- Renderer-facing API -----

    def show_run(self, run_id: str, *, label: str, body: str) -> None:
        self._frozen_runs.discard(run_id)
        self._fg_run_id = run_id
        self.query_one("#fg-body", Static).update(body)
        self.title = f"xana · {label}" if label else "xana"

    def update_run_body(self, run_id: str, body: str) -> None:
        if run_id != self._fg_run_id:
            return
        self.query_one("#fg-body", Static).update(body)

    def freeze_run(self, run_id: str, *, label: str) -> None:
        self._frozen_runs.add(run_id)
        if run_id == self._fg_run_id:
            self.query_one("#fg-body", Static).update(
                f"后台运行中 · {label}"
            )

    def finalize_run(self, run_id: str, *, body: str, label: str) -> None:
        self._frozen_runs.discard(run_id)
        if run_id == self._fg_run_id:
            self.query_one("#fg-body", Static).update(body)

    def refresh_tabs(
        self, bg_run_ids: list[str], *, labels: dict[str, str]
    ) -> None:
        if self._tabs_widget is None:
            return
        self._tabs_widget.clear()
        for rid in bg_run_ids:
            lbl = (labels.get(rid) or rid)[:24]
            prefix = "🟡 " if rid in self._frozen_runs else ""
            self._tabs_widget.append(_TabItem(rid, f"{prefix}{lbl}"))
