"""MCP 管理/状态 API(docs/superpowers/specs/2026-11-13-mcp-design.md §10)。

daemon 内的路由模块,不是独立服务:由 build_daemon_app 注册,与现有路由共用同一套
Bearer / Origin / Unix socket 中间件。请求体由 Pydantic 配置模型(McpServer /
McpServerOverride)校验;校验失败与异常映射为 400/404/409,错误消息不回显请求中的
env/header 等秘密,也不返回 pydantic 原文。响应只投影 manager 的脱敏 ServerView,
外加顶层 load_error(已脱敏)供热加载失败可见。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from xana.config.system_model import McpServer, McpServerOverride
from xana.mcp.contracts import ServerView
from xana.mcp.manager import McpManager


def _view_dict(view: ServerView) -> dict[str, Any]:
    return asdict(view)


def _servers_payload(manager: McpManager) -> dict[str, Any]:
    return {"servers": [_view_dict(v) for v in manager.list()],
            "load_error": manager.load_error}


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON object required")
    return body


def register_mcp_routes(app: web.Application, manager: McpManager) -> None:
    """在 daemon app 上注册 /mcp* 管理路由(设计 §10);调用方持有唯一 manager。"""

    async def list_servers(request: web.Request) -> web.Response:
        return web.json_response(_servers_payload(manager))

    async def register_server(request: web.Request) -> web.Response:
        body = await _json_object(request)
        try:
            config = McpServer.model_validate(body)
            view = await manager.register_server(config)
        except ValidationError:
            # 错误消息只给通用说明:ValidationError 原文会回显请求值(可能含秘密)
            return web.json_response({"error": "server 配置无效"}, status=400)
        except (ValueError, TypeError):
            return web.json_response({"error": "server 配置无效"}, status=400)
        except RuntimeError:
            return web.json_response({"error": "MCP manager 未运行"}, status=409)
        return web.json_response({"server": _view_dict(view)}, status=201)

    async def delete_server(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if all(v.name != name for v in manager.list()):
            return web.json_response({"error": "server not found"}, status=404)
        try:
            await manager.remove_server(name)   # 只删项目层;用户级定义随后恢复
        except RuntimeError:
            return web.json_response({"error": "MCP manager 未运行"}, status=409)
        return web.json_response(_servers_payload(manager))

    async def reload_server(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            await manager.reload(name)
        except KeyError:
            return web.json_response({"error": "server not found"}, status=404)
        except RuntimeError:
            return web.json_response({"error": "MCP manager 未运行"}, status=409)
        return web.json_response(_servers_payload(manager))

    async def reload_all(request: web.Request) -> web.Response:
        try:
            await manager.reload()
        except RuntimeError:
            return web.json_response({"error": "MCP manager 未运行"}, status=409)
        return web.json_response(_servers_payload(manager))

    async def set_availability(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await _json_object(request)
        try:
            override = McpServerOverride.model_validate(body)
        except ValidationError:
            return web.json_response({"error": "override 配置无效"}, status=400)
        except (ValueError, TypeError):
            return web.json_response({"error": "override 配置无效"}, status=400)
        try:
            view = await manager.set_availability(name, override)
        except KeyError:
            return web.json_response({"error": "server not found"}, status=404)
        except RuntimeError:
            return web.json_response({"error": "MCP manager 未运行"}, status=409)
        return web.json_response({"server": _view_dict(view)})

    app.router.add_get("/mcp/servers", list_servers)
    app.router.add_post("/mcp/servers", register_server)
    app.router.add_delete("/mcp/servers/{name}", delete_server)
    app.router.add_post("/mcp/servers/{name}/reload", reload_server)
    app.router.add_post("/mcp/reload", reload_all)
    app.router.add_put("/mcp/servers/{name}/availability", set_availability)
