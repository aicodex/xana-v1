# src/xana/cli/commands/mcp.py
"""xana mcp — MCP server 管理客户端(设计 §10 的轻量 CLI)。

只提供 list 与 reload:经现有 DaemonClient 调用运行中 serve 的 /mcp* 端点,
不在 CLI 里第二次 boot 或直接连接 manager。注册与细粒度工具选择由 Web 工作台完成。
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any, TextIO

from xana.cli._daemon_client import DaemonClient
from xana.cli.xanac import _resolve_endpoint


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=None, help="哪个项目的 serve(缺省当前项目)")
    sub = parser.add_subparsers(dest="mcp_action", required=True)

    sub.add_parser("list", help="列出 MCP server 状态、工具与审核结果")

    reload_p = sub.add_parser("reload", help="重连并重审 MCP server(或重读全部配置)")
    reload_p.add_argument("--server", default=None, help="只重载该 server;缺省重读全部配置")


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    if getattr(args, "project", None):
        os.environ["XANA_PROJECT"] = args.project
    return asyncio.run(_run(args, out=out))


async def _run(args: argparse.Namespace, *, out: TextIO) -> int:
    base_url, token, unix = _resolve_endpoint()
    client = DaemonClient(base_url=base_url, token=token, unix_socket=unix)
    try:
        if args.mcp_action == "list":
            data = await client.mcp_servers()
        else:
            data = await client.mcp_reload(args.server)
    finally:
        await client.close()
    _render(data, out=out)
    return 0


def _render(data: dict[str, Any], *, out: TextIO) -> None:
    servers = data.get("servers") or []
    load_error = data.get("load_error")
    if load_error:
        print(f"[xana mcp] 配置加载错误: {load_error}", file=out)
    if not servers:
        print("[xana mcp] 没有已配置的 MCP server", file=out)
        return
    for server in servers:
        line = (f"[xana mcp] {server['name']:<20} {server['origin']:<8} "
                f"{server['transport']:<6} {server['status']}")
        if server.get("error"):
            line += f"  error={server['error']}"
        if server.get("retry_in_s") is not None:
            line += f"  retry_in={server['retry_in_s']}s"
        print(line, file=out)
        for tool in server.get("tools") or []:
            print(f"  - {tool['name']:<40} {tool['status']}", file=out)
            for finding in tool.get("findings") or []:
                print(f"      ! {finding['rule']} {finding['path']} {finding['message']}", file=out)
