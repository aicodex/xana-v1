"""Feishu / Lark WebSocket inbound — long-connection mode (no public HTTPS).

Uses the official `lark-oapi` SDK. The bot opens an outbound WebSocket to
Feishu; events are pushed back over that connection. Equivalent to Telegram's
long-polling — works behind NAT, no need to configure a public HTTPS webhook
URL in the Feishu admin panel.

Why a separate file:
- `lark-oapi` is a heavy optional dep; importing it only when WS mode is
  selected keeps webhook-only deployments light (install via
  ``pip install xana[feishu-ws]``).
- The SDK's ``Client.start()`` is blocking, so we run it in a daemon thread
  and bridge events to the main asyncio loop via
  :func:`asyncio.run_coroutine_threadsafe`.

CRITICAL: lark_oapi must be imported INSIDE the worker thread. Its modules
run ``asyncio.get_event_loop()`` at import time and cache the result;
importing on the main asyncio thread would bind that cached loop and then
``Client.start()`` would crash with "event loop is already running".
Availability is pre-checked with :func:`importlib.util.find_spec`, which
does not execute module code.

Caveats:
- The lark SDK has no public ``stop()`` / ``close()``. :meth:`stop` just
  flips ``_stopping`` so in-flight callbacks short-circuit; the daemon
  thread dies with the process.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"

# Async callback invoked from the main loop with the bridged event dict.
# Shape matches the webhook event dict so callers can reuse
# ``feishu_events.parse_message_event`` unchanged.
EventCallback = Callable[[dict], Awaitable[None]]


class FeishuWSClient:
    """Lifetime-managed wrapper around ``lark_oapi.ws.client.Client``.

    Spawned by FeishuAdapter in WS connection mode. The blocking SDK call
    runs in a daemon thread; inbound events bridge back to the main asyncio
    loop via :func:`asyncio.run_coroutine_threadsafe`.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = DEFAULT_DOMAIN,
        on_im_message: EventCallback,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("FeishuWSClient requires app_id and app_secret")
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._on_im_message = on_im_message
        self._client = None  # lark_oapi.ws.client.Client (lazy, thread-local)
        self._thread: Optional[threading.Thread] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = False
        self._stopping = False

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the WebSocket daemon thread. Stores ``loop`` for bridging.

        Availability is checked via :func:`importlib.util.find_spec` (does
        NOT execute module code — see module docstring for why the real
        import must happen inside the worker thread).
        """
        if self._started:
            return
        import importlib.util

        if importlib.util.find_spec("lark_oapi") is None:
            raise RuntimeError(
                "lark-oapi is required for Feishu WebSocket mode. "
                "Install with: pip install lark-oapi "
                "(or pip install xana[feishu-ws])."
            )
        self._main_loop = loop
        self._thread = threading.Thread(
            target=self._run_thread, name="xana.feishu.ws", daemon=True
        )
        self._thread.start()
        self._started = True
        log.info(
            "feishu_ws: started (thread=%s, domain=%s)",
            self._thread.name,
            self._domain,
        )

    def thread_alive(self) -> bool:
        """Return True when the daemon thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def _run_thread(self) -> None:
        """Thread body: import lark HERE so its module-level event loop is
        bound to this thread. Then build dispatcher + client and block."""
        try:
            import lark_oapi as lark
            from lark_oapi.ws.client import Client as WSClient
        except Exception:
            log.exception("feishu_ws: failed to import lark-oapi in thread")
            return

        # Builder takes (encrypt_key, verification_token) but WS auth is
        # per-connection via app_id/app_secret, so empty strings are fine.
        try:
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_p2_im_message)
                .build()
            )
            self._client = WSClient(
                self._app_id,
                self._app_secret,
                event_handler=handler,
                domain=self._domain,
                auto_reconnect=True,
            )
        except Exception:
            log.exception("feishu_ws: failed to build lark client")
            return

        try:
            self._client.start()
        except Exception:
            log.exception("feishu_ws: thread crashed; auto-reconnect may rescue")

    def _on_p2_im_message(self, data) -> None:
        """Sync callback from the lark thread. Convert + bridge to async."""
        if self._stopping:
            return
        try:
            event_dict = _p2_im_message_to_event_dict(data)
        except Exception:
            log.exception("feishu_ws: failed to convert P2ImMessageReceiveV1")
            return
        loop = self._main_loop
        if loop is None or loop.is_closed():
            log.warning("feishu_ws: main loop unavailable; dropping event")
            return
        # Schedule on the main loop without blocking this thread — the SDK
        # expects callbacks to return promptly.
        try:
            asyncio.run_coroutine_threadsafe(self._invoke_callback(event_dict), loop)
        except Exception:
            log.exception("feishu_ws: run_coroutine_threadsafe failed")

    async def _invoke_callback(self, event_dict: dict) -> None:
        try:
            await self._on_im_message(event_dict)
        except Exception:
            log.exception("feishu_ws: event callback raised")

    async def stop(self) -> None:
        """Best-effort shutdown: flip ``_stopping`` so in-flight bridged
        callbacks short-circuit. The daemon thread dies with the process
        (the lark SDK exposes no public stop/close)."""
        self._stopping = True


def _p2_im_message_to_event_dict(data) -> dict:
    """Convert lark-oapi ``P2ImMessageReceiveV1`` into the webhook-event
    dict shape so :func:`feishu_events.parse_message_event` can consume it
    unchanged.

    Note: xana's parse_message_event does not read ``mentions``, but we keep
    them anyway for shape fidelity with real webhook payloads (cheap, and
    future mention-aware routing won't need a converter change).
    """
    msg = getattr(getattr(data, "event", None), "message", None)
    sender = getattr(getattr(data, "event", None), "sender", None)
    if msg is None:
        return {}

    mentions_out: list[dict] = []
    for m in getattr(msg, "mentions", None) or []:
        mid = getattr(m, "id", None)
        open_id = getattr(mid, "open_id", None) if mid is not None else None
        if open_id:
            mentions_out.append({"id": {"open_id": open_id}})

    sender_id = getattr(sender, "sender_id", None) if sender is not None else None
    sender_open_id = (
        getattr(sender_id, "open_id", None) if sender_id is not None else None
    )

    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_open_id} if sender_open_id else {},
            },
            "message": {
                "message_id": getattr(msg, "message_id", "") or "",
                "message_type": getattr(msg, "message_type", "") or "",
                "chat_id": getattr(msg, "chat_id", "") or "",
                "chat_type": getattr(msg, "chat_type", "p2p") or "p2p",
                "content": getattr(msg, "content", "") or "",
                "parent_id": getattr(msg, "parent_id", None),
                "mentions": mentions_out,
            },
        },
    }
