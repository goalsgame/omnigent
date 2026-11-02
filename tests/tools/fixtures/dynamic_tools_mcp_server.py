"""MCP server whose tool list follows a JSON state file.

Usage:

    python tests/tools/fixtures/dynamic_tools_mcp_server.py <state.json> stdio
    python tests/tools/fixtures/dynamic_tools_mcp_server.py <state.json> http <port>

``state.json`` holds ``{"tools": ["alpha", ...]}`` and is re-read on every
request, so a test changes the server's tools by rewriting the file. Each
listed tool returns ``"<name>: <text>"``; calling an unlisted tool fails.

The ``announce`` tool, when listed, adds ``arguments["add"]`` to the state
file and sends ``notifications/tools/list_changed`` on its session.

``http`` serves stateless streamable HTTP at ``http://127.0.0.1:<port>/mcp``,
the shape of a remote connector gateway that cannot push notifications
outside a request.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

_STATE = Path(sys.argv[1])

server: Server[object, object] = Server("dynamic-tools")


def _enabled() -> list[str]:
    return list(json.loads(_STATE.read_text())["tools"])


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=f"The {name} tool.",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}, "add": {"type": "string"}},
            },
        )
        for name in _enabled()
    ]


@server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    enabled = _enabled()
    if name not in enabled:
        raise ValueError(f"unknown tool: {name}")
    if name == "announce":
        _STATE.write_text(json.dumps({"tools": [*enabled, str(arguments["add"])]}))
        await server.request_context.session.send_tool_list_changed()
    return [types.TextContent(type="text", text=f"{name}: {arguments.get('text', '')}")]


async def _serve_stdio() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _serve_http(port: int) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.types import Receive, Scope, Send

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    class _Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint=_Endpoint())], lifespan=lifespan)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if sys.argv[2] == "stdio":
        anyio.run(_serve_stdio)
    else:
        _serve_http(int(sys.argv[3]))
