"""Approval controls stay reachable while the conversation worker is waiting."""
from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from xana.policy.approvals import PermissionService
from xana.policy.contracts import PermissionContext
from xana.workbench.conversations import ConversationService
from xana.runtime.workbench_http import require_session


def register_approval_routes(app: web.Application, permissions: PermissionService,
                             conversations: ConversationService) -> None:
    async def setting(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        if request.method == "POST":
            try:
                body = await request.json()
                await permissions.configure(sid, body["mode"])
            except (ValueError, KeyError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc))
        context, _ = await permissions.current(PermissionContext("yolo", sid))
        return web.json_response({"mode": context.mode, "plan_mode": context.plan_mode})

    async def pending(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        return web.json_response({"approvals": [asdict(item) for item in permissions.pending(sid)]})

    async def respond(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("scope", "once") not in {"once", "session"}:
                raise ValueError("invalid approval scope")
            result = await permissions.respond(sid, request.match_info["approval"], body["decision"],
                                               session_scope=body.get("scope") == "session")
        except KeyError:
            raise web.HTTPBadRequest(text="approval decision required")
        except LookupError:
            raise web.HTTPNotFound(text="approval not found")
        except (ValueError, TypeError) as exc:
            raise web.HTTPConflict(text=str(exc))
        return web.json_response(asdict(result))

    app.router.add_get("/sessions/{sid}/permissions", setting)
    app.router.add_post("/sessions/{sid}/permissions", setting)
    app.router.add_get("/sessions/{sid}/approvals", pending)
    app.router.add_post("/sessions/{sid}/approvals/{approval}", respond)
