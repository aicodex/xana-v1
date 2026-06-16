"""xana daemon HTTP+SSE server.

build_daemon_app(bridge, token, *, on_action) -> aiohttp.web.Application
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from aiohttp import web

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter

_logger = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
_SERVER_VERSION = "0.1.0"


def build_daemon_app(
    *,
    bridge: DaemonBridgeAdapter,
    token: str,
    on_action: Callable[[str, str, dict], Awaitable[None]] | None = None,
) -> web.Application:
    """Build the aiohttp app for the xana daemon.

    on_action(sid, action, payload) is invoked for cancel/attach/detach/button
    POSTs; the host (serve.py) wires this to gateway.cancel/attach/detach /
    Card.dispatch as appropriate. None -> 202 with no side effect.
    """
    app = web.Application()

    @web.middleware
    async def origin_reject(request, handler):
        if request.headers.get("Origin"):
            return web.json_response(
                {"error": "Origin header rejected"}, status=403)
        return await handler(request)

    @web.middleware
    async def auth(request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if not token:
            return await handler(request)
        got = request.headers.get("Authorization", "")
        if got != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.extend([origin_reject, auth])

    async def healthz(_request):
        return web.json_response({"ok": True, "version": _SERVER_VERSION})

    async def submit(request):
        sid = request.match_info["sid"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        text = str(body.get("text", "") or "")
        await bridge.submit_inbound(sid, text, body.get("metadata"))
        return web.json_response({}, status=202)

    async def stream(request):
        sid = request.match_info["sid"]
        last_id_int = _parse_last_event_id(
            request.headers.get("Last-Event-ID"))

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        init_payload = {
            "server_version": _SERVER_VERSION,
            "protocol_version": _PROTOCOL_VERSION,
            "supported_methods": ["agent_event", "progress",
                                  "session.status", "turn.interrupted"],
        }
        await _write_sse(resp, seq=0, evt="initialize", data=init_payload)

        try:
            async for seq, evt_type, data in bridge.subscribe(
                sid, last_event_id=last_id_int):
                await _write_sse(resp, seq=seq, evt=evt_type, data=data)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        return resp

    async def action(request):
        sid = request.match_info["sid"]
        kind = request.match_info["action"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if on_action is not None:
            asyncio.create_task(on_action(sid, kind, body))
        return web.json_response({}, status=202)

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/sessions/{sid}/submit", submit)
    app.router.add_get("/sessions/{sid}/stream", stream)
    app.router.add_post(
        "/sessions/{sid}/{action:cancel|attach|detach|button}", action)
    return app


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _write_sse(resp: web.StreamResponse, *, seq: int, evt: str,
                     data: dict) -> None:
    chunk = (
        f"id: {seq}\n"
        f"event: {evt}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")
    await resp.write(chunk)
