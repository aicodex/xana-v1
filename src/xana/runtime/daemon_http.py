"""xana daemon HTTP+SSE server.

build_daemon_app(bridge, token, *, on_action) -> aiohttp.web.Application
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

from aiohttp import web

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.multimodal.files import MAX_ATTACHMENTS
from xana.workbench.conversations import ConversationService
from xana.runtime.workbench_http import register_workbench_routes
from xana.policy.approvals import PermissionService
from xana.runtime.approval_http import register_approval_routes
from xana.state.todos import TodoStore
from xana.harness_sdk.goal import GoalService
from xana.harness_sdk.turn_control import TurnCoordinator
from xana.runtime.goal_http import register_goal_routes
from xana.runtime.web_assets import register_web_assets
from xana.harness_sdk.background import BackgroundTasks
from xana.state.process_logs import ProcessLogs
from xana.runtime.process_log_http import register_process_log_routes
from xana.workbench.media import MediaUploads
from xana.runtime.workbench_http import require_session

_logger = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
_SERVER_VERSION = "0.1.0"


def build_daemon_app(
    *,
    bridge: DaemonBridgeAdapter,
    token: str,
    on_action: Callable[[str, str, dict], Coroutine[Any, Any, None]] | None = None,
    heartbeat_interval_s: float = 15.0,
    idle_unload_s: float = 1800.0,
    conversations: ConversationService | None = None,
    permissions: PermissionService | None = None,
    todos: TodoStore | None = None,
    goals: GoalService | None = None,
    control: TurnCoordinator | None = None,
    background: BackgroundTasks | None = None,
    process_logs: ProcessLogs | None = None,
    media_uploads: MediaUploads | None = None,
) -> web.Application:
    """Build the aiohttp app for the xana daemon.

    on_action(sid, action, payload) is invoked for cancel/button
    POSTs; the host (serve.py) wires this to gateway.cancel /
    Card.dispatch as appropriate. None -> 202 with no side effect.
    """
    app = web.Application()

    @web.middleware
    async def origin_reject(request, handler):
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return web.json_response({"error": "Origin header rejected"}, status=403)
            if (not token or parsed.scheme != request.scheme
                    or parsed.netloc != request.host or parsed.path or parsed.query or parsed.fragment):
                return web.json_response({"error": "Origin header rejected"}, status=403)
        return await handler(request)

    @web.middleware
    async def auth(request, handler):
        if request.path == "/healthz" or request.match_info.route.name in {"workbench.index", "workbench.asset"}:
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
        if not isinstance(body, dict):
            return web.json_response({"error": "JSON object required"}, status=400)
        text = str(body.get("text", "") or "")
        attachments = body.get("attachments", [])
        if not isinstance(attachments, list):
            return web.json_response({"error": "attachments must be a list"}, status=400)
        if request.headers.get("Origin") or any(isinstance(item, dict) and "upload_id" in item for item in attachments):
            try:
                if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
                    raise ValueError("invalid attachments")
                attachments = [media_uploads.attachment(sid, item["upload_id"]) for item in attachments] if media_uploads else ([] if not attachments else None)
                if attachments is None:
                    raise ValueError("media upload service required")
            except (ValueError, KeyError, TypeError, LookupError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
        if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS or any(
            not isinstance(item, dict) or not isinstance(item.get("file_path"), str)
            or not item["file_path"] for item in attachments
        ):
            return web.json_response({"error": f"attachments must be up to {MAX_ATTACHMENTS} file_path objects"}, status=400)
        await bridge.submit_inbound(sid, text, body.get("metadata"), attachments=tuple(attachments))
        return web.json_response({}, status=202)

    async def media(request):
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        try:
            if request.method == "POST":
                return web.json_response(await media_uploads.upload(sid, request), status=201)
            return web.json_response({"input_modalities": await media_uploads.modalities(sid)})
        except (ValueError, LookupError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    if media_uploads is not None and conversations is not None:
        app.router.add_get("/sessions/{sid}/media", media)
        app.router.add_post("/sessions/{sid}/media", media)

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

        bridge.note_subscribe(sid)
        status = bridge.session_status(sid)
        # Account for this client which has not yet entered subscribe()
        status["subscribers"] = status["subscribers"] + 1
        status["state"] = "active"
        await _write_sse(resp, seq=0, evt="session.status", data=status)

        hb_task = asyncio.create_task(_heartbeat(resp, heartbeat_interval_s))
        try:
            async for seq, evt_type, data in bridge.subscribe(
                sid, last_event_id=last_id_int):
                await _write_sse(resp, seq=seq, evt=evt_type, data=data)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            hb_task.cancel()
            bridge.note_unsubscribe(sid)
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

    async def resolve_sid(request):
        # Returns the SessionResolver's current sid for (adapter, thread).
        # Used by clients (xanac) to follow /clear rotations: before each
        # submit, ask which sid we should use right now.
        # Query: ?adapter=cli&thread=  (thread defaults to "")
        adapter_id = request.query.get("adapter", "cli")
        thread_id = request.query.get("thread", "")
        sid = bridge.resolve_session(thread_id) if adapter_id == "cli" else None
        if sid is None:
            return web.json_response(
                {"error": "resolver unavailable for adapter"}, status=404,
            )
        return web.json_response({
            "adapter": adapter_id, "thread": thread_id, "session_id": sid,
        })

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/resolve", resolve_sid)
    app.router.add_post("/sessions/{sid}/submit", submit)
    app.router.add_get("/sessions/{sid}/stream", stream)
    app.router.add_post(
        "/sessions/{sid}/{action:cancel|button}", action)
    if conversations is not None:
        register_workbench_routes(app, conversations, permissions=permissions, todos=todos,
                                 goals=goals, background=background, control=control)
        if permissions is not None:
            register_approval_routes(app, permissions, conversations)
        if goals is not None:
            if control is None:
                raise ValueError("goal controls require the conversation coordinator")
            register_goal_routes(app, goals, conversations, bridge, control)
        if process_logs is not None:
            register_process_log_routes(app, process_logs, conversations)
    register_web_assets(app)

    async def _start_idle(app):
        app["_idle_task"] = asyncio.create_task(_idle_loop(
            bridge, idle_unload_s=idle_unload_s))

    async def _stop_idle(app):
        t = app.get("_idle_task")
        if t:
            t.cancel()

    app.on_startup.append(_start_idle)
    app.on_cleanup.append(_stop_idle)
    return app


async def _idle_loop(bridge: DaemonBridgeAdapter, *, idle_unload_s: float):
    interval = max(1.0, min(idle_unload_s / 2 if idle_unload_s > 0 else 30, 60))
    while True:
        await asyncio.sleep(interval)
        bridge.sweep_idle(idle_unload_s)


async def _heartbeat(resp, interval_s):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await resp.write(b": heartbeat\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            return


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


async def run_unix_socket(app: web.Application, path: str) -> None:
    """Run app on a Unix socket at `path` with 0600 perm. Block until cancelled."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, path)
    await site.start()
    os.chmod(path, 0o600)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


async def run_tcp(app: web.Application, host: str, port: int) -> None:
    """Run app on TCP. Block until cancelled."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
