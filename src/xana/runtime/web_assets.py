"""Serve the packaged workbench; browser API calls still require authentication."""
from pathlib import Path

from aiohttp import web


def register_web_assets(app: web.Application) -> None:
    root = Path(__file__).resolve().parents[1] / "workbench" / "static"

    async def index(request: web.Request) -> web.FileResponse:
        path = root / "index.html"
        if not path.is_file():
            raise web.HTTPServiceUnavailable(text="Web assets are not built; run npm ci and npm run build in web/")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache", "Referrer-Policy": "no-referrer"})

    async def asset(request: web.Request) -> web.FileResponse:
        directory = (root / "assets").resolve()
        path = (directory / request.match_info["name"]).resolve()
        if path.parent != directory or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable",
                                               "X-Content-Type-Options": "nosniff"})

    app.router.add_get("/", index, name="workbench.index")
    app.router.add_get("/assets/{name}", asset, name="workbench.asset")
