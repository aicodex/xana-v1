"""Paged and streaming command logs, scoped to the recorded session and run."""
from contextlib import closing
from dataclasses import asdict, replace

from aiohttp import web

from xana.state.process_logs import ProcessLogs
from xana.workbench.conversations import ConversationService


def register_process_log_routes(app: web.Application, logs: ProcessLogs, conversations: ConversationService) -> None:
    async def read(request: web.Request) -> web.StreamResponse:
        sid, rid, execution = (request.match_info[key] for key in ("sid", "run", "execution"))
        try:
            run = await conversations.run(sid, rid)
            offset = int(request.query["offset"]) if "offset" in request.query else None
            before = int(request.query["before"]) if "before" in request.query else None
            page = logs.read(execution, session_id=sid, run_id=rid, offset=offset, before=before)
            if run.status != "running" and not page.complete:
                page = replace(page, complete=True, issue=page.issue or "execution ended without a log end marker")
        except LookupError:
            raise web.HTTPNotFound(text="log unavailable")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if request.match_info.route.name == "workbench.log_download":
            response = web.StreamResponse(headers={"Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": 'attachment; filename="command.log"'})
            await response.prepare(request)
            with closing(logs.text(execution, session_id=sid, run_id=rid)) as chunks:
                for text in chunks:
                    await response.write(text.encode())
            return response
        return web.json_response(asdict(page))

    app.router.add_get("/sessions/{sid}/runs/{run}/logs/{execution}", read)
    app.router.add_get("/sessions/{sid}/runs/{run}/logs/{execution}/download", read, name="workbench.log_download")
