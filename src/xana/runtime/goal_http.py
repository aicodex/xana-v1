"""Explicit, session-scoped goal controls sharing ordinary conversation admission."""
from __future__ import annotations

from aiohttp import web

from xana.agent_adapter.channels.daemon_bridge import DaemonBridgeAdapter
from xana.harness_sdk.goal import GoalService, GOAL_CONTINUATION
from xana.harness_sdk.turn_control import TurnCoordinator
from xana.runtime.workbench_http import require_session
from xana.workbench.conversations import ConversationService


def register_goal_routes(app: web.Application, goals: GoalService, conversations: ConversationService,
                         bridge: DaemonBridgeAdapter, control: TurnCoordinator) -> None:
    async def get_goal(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        return web.json_response({"goal": goals.view(sid)})

    async def change_goal(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        await require_session(conversations, sid)
        try:
            body = await request.json()
            action = body["action"]
            if action == "start":
                goal = goals.create(sid, body["objective"], token_budget=body.get("token_budget"),
                                    completion_criterion=body.get("completion_criterion", ""))
                await bridge.submit_inbound(sid, goal.objective, {"input_id": f"goal:{goal.goal_id}:start"})
            elif action in {"pause", "cancel"}:
                if action == "pause":
                    goals.update(sid, "paused", reason="user")
                else:
                    goals.cancel(sid)
                await control.cancel(sid, wait=True)
            elif action == "resume":
                goal = goals.update(sid, "active", reason="user")
                await bridge.submit_inbound(sid, GOAL_CONTINUATION,
                    {"source": "goal", "input_id": f"goal:{goal.goal_id}:resume:{goal.revision}"})
            else:
                raise ValueError("invalid goal action")
        except KeyError:
            raise web.HTTPBadRequest(text="goal action and objective required")
        except LookupError:
            raise web.HTTPNotFound(text="no current goal")
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"goal": goals.view(sid)})

    app.router.add_get("/sessions/{sid}/goal", get_goal)
    app.router.add_post("/sessions/{sid}/goal", change_goal)
