"""Browser conversation queries and replay over the existing daemon service."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

from aiohttp import web

from xana.workbench.conversations import ConversationService
from xana.policy.approvals import PermissionService
from xana.state.todos import TodoStore
from xana.harness_sdk.goal import GoalService
from xana.harness_sdk.background import BackgroundTasks
from xana.harness_sdk.turn_control import TurnCoordinator
from pathlib import Path


def register_workbench_routes(app: web.Application, conversations: ConversationService,
                             *, permissions: PermissionService | None = None, todos: TodoStore | None = None,
                             goals: GoalService | None = None, background: BackgroundTasks | None = None,
                             control: TurnCoordinator | None = None) -> None:
    async def sessions(request: web.Request) -> web.Response:
        return web.json_response({"sessions": [asdict(row) for row in await conversations.sessions()]})

    async def create(request: web.Request) -> web.Response:
        return web.json_response({"session_id": await conversations.create()}, status=201)

    async def workspace(request: web.Request) -> web.Response:
        return web.json_response({"name": Path(conversations.workspace).name or "工作区",
                                  "path": conversations.workspace})

    async def tasks(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        if background is None:
            raise web.HTTPServiceUnavailable(text="background service required")
        rows = sorted(background.list_for_session(sid, include_finished=True),
                      key=lambda row: (row.status == "running", row.started, row.id), reverse=True)
        before = request.query.get("before")
        if before:
            position = next((index for index, row in enumerate(rows) if row.id == before), None)
            if position is None:
                raise web.HTTPBadRequest(text="unknown task cursor")
            rows = rows[position + 1:]
        selected = []
        for row in rows[:50]:
            data = row.to_json()
            data.pop("rendered")
            data["content"] = {key: row.content[key] for key in ("agent_id", "agent") if key in row.content}
            data["progress"] = list(row.progress)
            selected.append(data)
        return web.json_response({"tasks": selected, "has_more": len(rows) > 50,
            "next_before": rows[49].id if len(rows) > 50 else None})

    async def stop_task(request: web.Request) -> web.Response:
        sid, ident = request.match_info["sid"], request.match_info["task"]
        await require_session(conversations, sid)
        if background is None:
            raise web.HTTPServiceUnavailable(text="background service required")
        if background.get(ident, session_id=sid) is None:
            raise web.HTTPNotFound(text="task not found")
        return web.json_response({"stopped": await background.stop(ident)})

    async def steer(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        try:
            await control.promote(sid, request.match_info["input"])
        except (LookupError, ValueError) as exc:
            raise web.HTTPConflict(text=str(exc))
        return web.json_response({"delivery": "steer"})

    async def conversation(request: web.Request) -> web.Response:
        try:
            page = await conversations.page(request.match_info["sid"],
                before=request.query.get("before"), limit=int(request.query.get("limit", "20")))
        except LookupError:
            raise web.HTTPNotFound(text="session not found")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(asdict(page))

    async def checklist(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        try:
            await conversations.require_session(sid)
        except LookupError:
            raise web.HTTPNotFound(text="session not found")
        if todos is None:
            raise web.HTTPServiceUnavailable(text="checklist store required")
        return web.json_response({"items": [asdict(item) for item in todos.load(sid)]})

    async def events(request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        try:
            await conversations.require_session(sid)
        except LookupError:
            raise web.HTTPNotFound(text="session not found")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        subscription = conversations.events.subscribe(sid, after=request.headers.get("Last-Event-ID"))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        try:
            # Subscribe before querying history, so commits during the query remain queued.
            snapshot = await conversations.page(sid, observation=subscription) if subscription.reset else None
            await response.prepare(request)
            if snapshot is not None:
                payload = asdict(snapshot)
                if permissions is not None:
                    payload["approvals"] = [asdict(item) for item in permissions.pending(sid)]
                if todos is not None:
                    payload["checklist"] = [asdict(item) for item in todos.load(sid)]
                if goals is not None:
                    payload["goal"] = goals.view(sid)
                await _send(response, subscription.cursor, "snapshot", payload)
            else:
                for record in subscription.replay:
                    await _send(response, record.cursor, "execution", asdict(record.event))
                await _send(response, subscription.cursor, "ready", {})
            while True:
                try:
                    record = await asyncio.wait_for(subscription.receive(), timeout=15)
                except asyncio.TimeoutError:
                    await response.write(b": heartbeat\n\n")
                    continue
                if record is None:
                    break  # Bounded queue overflow: reconnect replays or resets from history.
                await _send(response, record.cursor, "execution", asdict(record.event))
        except ConnectionResetError:
            pass
        finally:
            subscription.close()
        return response

    app.router.add_get("/sessions", sessions)
    app.router.add_post("/sessions", create)
    app.router.add_get("/workspace", workspace)
    app.router.add_get("/sessions/{sid}/conversation", conversation)
    app.router.add_get("/sessions/{sid}/events", events)
    if control is not None:
        app.router.add_post("/sessions/{sid}/inputs/{input}/steer", steer)
    if todos is not None:
        app.router.add_get("/sessions/{sid}/checklist", checklist)
    if background is not None:
        app.router.add_get("/sessions/{sid}/tasks", tasks)
        app.router.add_post("/sessions/{sid}/tasks/{task}/stop", stop_task)


async def _send(response: web.StreamResponse, cursor: str, kind: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    await response.write(f"id: {cursor}\nevent: {kind}\ndata: {data}\n\n".encode())


async def require_session(conversations: ConversationService, sid: str) -> None:
    """Translate conversation lookup failures at the HTTP boundary."""
    try:
        await conversations.require_session(sid)
    except LookupError:
        raise web.HTTPNotFound(text="session not found")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
