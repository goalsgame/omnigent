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


async def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task, sock


def _bridge_params(coords, setup: str = "") -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "import sys\nfrom pathlib import Path\nimport anyio\n"
            "import omnigent.host.mcp_identity as bridge\n"
            f"{setup}"
            "bridge._coords_path = lambda: Path(sys.argv[1])\nanyio.run(bridge.main)",
            str(coords),
        ],
    )


def _write_coords(tmp_path, sock):
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
    return coords


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_runner_recovers_after_one_upstream_error(tmp_path, status):
    from mcp.shared.exceptions import McpError

    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.spec.types import AgentSpec, MCPServerConfig

    mcp = FastMCP("fixture", streamable_http_path="/v1/hosts/host-one/mcp", stateless_http=True)

    @mcp.tool()
    def echo(value: str) -> str:
        return value

    app = mcp.streamable_http_app()
    fail_next = []
    posts = []

    async def flaky(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            posts.append(scope["path"])
            if fail_next:
                fail_next.clear()
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"MCP upstream unavailable"})
                return
        await app(scope, receive, send)

    server, task, sock = await _serve(flaky)
    params = _bridge_params(_write_coords(tmp_path, sock))
    manager = RunnerMcpManager(stdio_cwd=tmp_path)
    spec = AgentSpec(
        spec_version=1,
        name="fixture",
        mcp_servers=[
            MCPServerConfig(
                name="connectors", transport="stdio", command=params.command, args=params.args
            )
        ],
    )
    try:
        async with asyncio.timeout(30):
            assert await manager.call_tool(spec, "connectors__echo", {"value": "a"}) == "a"
            fail_next.append(True)
            with pytest.raises(McpError, match=f"HTTP {status}"):
                await manager.call_tool(spec, "connectors__echo", {"value": "b"})
            sent = len(posts)
            assert await manager.call_tool(spec, "connectors__echo", {"value": "c"}) == "c"
            assert len(posts) > sent
    finally:
        await manager.shutdown()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@pytest.mark.asyncio
async def test_stdio_bridge_exits_when_its_relay_fails(tmp_path):
    from mcp.shared.exceptions import McpError

    mcp = FastMCP("fixture", streamable_http_path="/v1/hosts/host-one/mcp", stateless_http=True)

    @mcp.tool()
    def echo(value: str) -> str:
        return value

    server, task, sock = await _serve(mcp.streamable_http_app())
    params = _bridge_params(
        _write_coords(tmp_path, sock),
        "forward = bridge._IdentityTransport.handle_async_request\n"
        "async def crash(self, request):\n"
        "    if request.method == 'POST' and b'tools/call' in request.content:\n"
        "        raise RuntimeError('relay crashed')\n"
        "    return await forward(self, request)\n"
        "bridge._IdentityTransport.handle_async_request = crash\n",
    )
    try:
        async with (
            asyncio.timeout(30),
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            with pytest.raises(McpError, match="Connection closed"):
                await session.call_tool("echo", {"value": "lost"})
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()
