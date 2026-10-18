"""Exercise the runner's stdio bridge against an actual HTTP MCP server."""

import asyncio
import json
import socket
import sys

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP


@pytest.mark.asyncio
async def test_stdio_bridge_initializes_lists_and_calls_tools(tmp_path):
    mcp = FastMCP("fixture", streamable_http_path="/v1/hosts/host-one/mcp")

    @mcp.tool()
    def echo(value: str) -> str:
        return value

    app = mcp.streamable_http_app()
    seen = []

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            assert headers[b"x-omnigent-host-token"] == b"fixture-host-token"
            assert b"authorization" not in headers
            seen.append(scope["method"])
        await app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(authenticated, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                await asyncio.sleep(0.01)
            (tmp_path / ".omnigent-inference-broker.json").write_text(
                json.dumps(
                    {
                        "server": f"http://127.0.0.1:{sock.getsockname()[1]}",
                        "host_id": "host-one",
                        "host_token": "fixture-host-token",
                    }
                )
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-c",
                    "import sys; from pathlib import Path; import anyio; "
                    "import omnigent.host.mcp_identity as bridge; "
                    "bridge._coords_path = lambda: Path(sys.argv[1]); anyio.run(bridge.main)",
                    str(tmp_path / ".omnigent-inference-broker.json"),
                ],
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["echo"]
                result = await session.call_tool("echo", {"value": "owner-scoped tool works"})
                assert not result.isError
                assert result.content[0].text == "owner-scoped tool works"
            assert "POST" in seen
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("through_runner", [False, True])
async def test_stdio_bridge_reports_expired_identity_without_timeout(
    tmp_path, status, through_runner
):
    from fastapi import FastAPI
    from starlette.responses import JSONResponse

    app = FastAPI()

    @app.post("/v1/hosts/host-one/mcp")
    def reject():
        return JSONResponse({"detail": "fixture identity expired"}, status_code=status)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
            coords = tmp_path / "coords.json"
            coords.write_text(
                json.dumps(
                    {
                        "server": f"http://127.0.0.1:{sock.getsockname()[1]}",
                        "host_id": "host-one",
                        "host_token": "fixture",
                    }
                )
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-c",
                    "import sys; from pathlib import Path; import anyio; "
                    "import omnigent.host.mcp_identity as bridge; "
                    "bridge._coords_path = lambda: Path(sys.argv[1]); anyio.run(bridge.main)",
                    str(coords),
                ],
            )
            if through_runner:
                from omnigent.runner.mcp_manager import RunnerMcpManager
                from omnigent.spec.types import AgentSpec, MCPServerConfig

                manager = RunnerMcpManager(stdio_cwd=str(tmp_path))
                spec = AgentSpec(
                    spec_version=1,
                    name="fixture",
                    mcp_servers=[
                        MCPServerConfig(
                            name="identity",
                            transport="stdio",
                            command=params.command,
                            args=params.args,
                        )
                    ],
                )
                try:
                    result = await manager.schemas_for(spec)
                    assert result.failures == {
                        "identity": "Sign in again to restore connector access"
                    }
                finally:
                    await manager.shutdown()
                return
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                from mcp.shared.exceptions import McpError

                with pytest.raises(McpError, match="Sign in again to restore connector access"):
                    await session.initialize()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()
