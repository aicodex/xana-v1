"""xanac: HTTP+SSE thin client to xana serve daemon.

Replaces the previous in-process `boot()`-per-call architecture. xanac now:
  1. Resolves the daemon endpoint from ~/.xana/config/system.json (unix
     socket preferred, falls back to TCP).
  2. If not listening: subprocess.Popen("xana serve") and poll healthz.
  3. Oneshot path: POST /submit + consume SSE events, accumulate into a
     UnifiedCardState, print once on terminal event (final/error/cancelled).
  4. Interactive path: delegate to xana.cli.xanac_textual.run_textual_with_daemon
     (Task 11).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from xana.agent_adapter.channels.unified_card import UnifiedCardState
from xana.agent_adapter.channels.unified_card_events import apply_event
from xana.agent_adapter.channels.unified_card_cli import render as render_card
from xana.cli._daemon_client import DaemonClient
from xana.config.system_model import SystemConfig


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="xanac", description="xana CLI client")
    p.add_argument("text", nargs="*", help="one-shot prompt")
    p.add_argument("--session", default="default")
    p.add_argument("--random-session", action="store_true")
    p.add_argument("-i", "--interactive", action="store_true")
    return p.parse_args(argv)


def _resolve_endpoint() -> tuple[str, str, str | None]:
    """Returns (base_url, token, unix_socket_path or None)."""
    home = Path.home() / ".xana"
    sys_json = home / "config" / "system.json"
    cfg = SystemConfig()
    if sys_json.exists():
        try:
            cfg = SystemConfig.model_validate(
                json.loads(sys_json.read_text() or "{}"))
        except Exception:
            cfg = SystemConfig()
    d = cfg.daemon
    unix = Path(d.unix_socket).expanduser() if d.unix_socket else None
    if unix and unix.exists():
        return ("http://unix", "", str(unix))
    if d.bind:
        return (f"http://{d.bind}:{d.port}", d.token, None)
    return (f"http://127.0.0.1:{d.port}", d.token, None)


def _is_listening(unix_path: str | None, host: str, port: int) -> bool:
    if unix_path and Path(unix_path).exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(unix_path)
            s.close()
            return True
        except OSError:
            return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def _ensure_serve_running() -> None:
    base_url, token, unix = _resolve_endpoint()
    del token  # only base_url + unix needed here; token used by callers
    if unix and Path(unix).exists() and _is_listening(unix, "", 0):
        return
    netloc = base_url.replace("http://", "").split("/", 1)[0]
    if ":" in netloc and netloc != "unix":
        host, port_s = netloc.split(":", 1)
    else:
        host, port_s = "127.0.0.1", "17777"
    if _is_listening(unix, host, int(port_s)):
        return
    log = Path.home() / ".xana" / "logs" / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    xana_bin = Path.home() / ".xana" / "bin" / "xana"
    if not xana_bin.exists():
        xana_bin_str = "xana"
    else:
        xana_bin_str = str(xana_bin)
    subprocess.Popen(
        [xana_bin_str, "serve"],
        stdout=f, stderr=f, start_new_session=True,
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _is_listening(unix, host, int(port_s)):
            return
        time.sleep(0.2)
    try:
        tail = log.read_text().splitlines()[-20:]
        msg = "\n".join(tail)
    except Exception:
        msg = "(log unavailable)"
    raise RuntimeError(
        f"xana serve failed to come up within 20s. log tail ({log}):\n{msg}")


def _evt_from_wire(data: dict):
    """Reconstruct AgentEvent from SSE wire payload (mirror of
    daemon_bridge._agent_event_to_dict)."""
    from xana.agents.contracts import AgentEvent
    from xana.harness_sdk.contracts import HarnessEvent
    he = data.get("harness_event")
    harness_event = None
    if he:
        harness_event = HarnessEvent(
            kind=he["kind"], session_id=he["session_id"],
            run_id=he["run_id"], payload=he.get("payload") or {})
    return AgentEvent(
        kind=data["kind"], session_id=data["session_id"],
        payload=data.get("payload") or {}, harness_event=harness_event,
    )


def _progress_from_wire(data: dict):
    """Reconstruct ProgressEvent from SSE wire payload (gateway.note_progress
    payloads carry kind/session_id/run_id/... — see gateway.py)."""
    from xana.harness_sdk.contracts import ProgressEvent
    payload = dict(data)
    return ProgressEvent(
        session_id=str(payload.get("session_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        message=str(payload.get("kind") or ""),
        payload=payload,
    )


async def _oneshot_run(
    *, base_url: str, token: str, session_id: str, text: str,
    out: TextIO, timeout_s: float = 120.0, unix_socket: str | None = None,
) -> int:
    """Submit `text`, consume SSE until final/error/cancelled, print card."""
    client = DaemonClient(base_url=base_url, token=token,
                          unix_socket=unix_socket)
    try:
        state = UnifiedCardState(card_id=session_id, ph_run_id=session_id)
        done = asyncio.Event()
        rc = 0
        # Replay-vs-live gate: only honour final/error/cancelled for events
        # received AFTER our submit's baseline seq. Replay can deliver an
        # earlier turn's final ahead of our /compact (or new prompt)
        # progress events; without this we'd exit before consuming them.
        baseline_seq = -1

        async def reader():
            nonlocal rc, state
            async for ev in client.stream(session_id):
                if ev.event == "initialize":
                    continue
                if ev.event == "session.status":
                    continue
                if ev.event == "replay_gap":
                    state = UnifiedCardState(
                        card_id=session_id, ph_run_id=session_id)
                    continue
                if ev.event == "agent_event":
                    aev = _evt_from_wire(ev.data)
                    # Ensure terminal events surface even when no prior
                    # turn_started/model_delta was observed (e.g. cached
                    # replay starting partway through, or short server
                    # responses): seed an AnswerSection so apply_event's
                    # `final` branch has something to finalize.
                    he = aev.harness_event
                    if (he is not None and he.kind in ("final", "error")
                            and state.answer is None):
                        from xana.agent_adapter.channels.unified_card import (
                            AnswerSection,
                        )
                        state.answer = AnswerSection()
                    apply_event(state, aev)
                    kind = ev.data.get("kind")
                    if kind in ("final", "error", "cancelled"):
                        seq = getattr(ev, "seq", 0) or 0
                        if baseline_seq < 0 or seq <= baseline_seq:
                            # Replay-phase terminator for a prior turn; skip
                            continue
                        if kind == "error":
                            rc = 1
                        done.set()
                        return
                    continue
                if ev.event == "progress":
                    # detach / ph_replaced / compact_* / command_progress
                    # arrive as ProgressEvent via gateway.note_progress; if
                    # dropped, /compact cards don't freeze and bg cards
                    # don't get terminal patches.
                    apply_event(state, _progress_from_wire(ev.data))
                    # /compact run finishes via compact_finalized progress
                    # (no agent_event=final for compact). Honour it as a
                    # oneshot terminator alongside final/error/cancelled,
                    # gated by the same baseline check.
                    kind_p = ev.data.get("kind")
                    if kind_p in ("compact_finalized", "compact_cancelled"):
                        seq = getattr(ev, "seq", 0) or 0
                        if baseline_seq >= 0 and seq > baseline_seq:
                            done.set()
                            return
                    continue

        reader_task = asyncio.create_task(reader())
        # Let replay phase drain so baseline_seq snapshots only pre-existing
        # events; afterwards, submit our prompt and treat terminations only
        # for events with seq > baseline.
        await asyncio.sleep(0.5)
        baseline_seq = client._last_seq.get(session_id, 0)
        await client.submit(session_id, text)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            rc = 2
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        out.write(render_card(state) + "\n")
        return rc
    finally:
        await client.close()


def _session_id(args: argparse.Namespace) -> str:
    if args.random_session:
        return f"cli-{secrets.token_hex(3)}"
    return args.session


async def _async_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    text = " ".join(args.text).strip()
    if args.interactive or (not text and sys.stdin.isatty()):
        from xana.cli.xanac_textual import run_textual_with_daemon
        return await run_textual_with_daemon(session_id=_session_id(args))
    _ensure_serve_running()
    base_url, token, unix = _resolve_endpoint()
    return await _oneshot_run(
        base_url=base_url, token=token,
        session_id=_session_id(args), text=text,
        out=sys.stdout, unix_socket=unix,
    )


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
