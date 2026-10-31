"""Exercise the runner's stdio bridge against an actual HTTP MCP server."""

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager

import httpx
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


@asynccontextmanager
async def _serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    raise RuntimeError("fixture server exited before startup")
                await asyncio.sleep(0.01)
        yield sock
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


def _echo_app(json_response: bool = False):
    mcp = FastMCP(
        "fixture",
        streamable_http_path="/v1/hosts/host-one/mcp",
        stateless_http=True,
        json_response=json_response,
    )

    @mcp.tool()
    def echo(value: str) -> str:
        return value

    return mcp.streamable_http_app()


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


@asynccontextmanager
async def _runner_manager(tmp_path, params):
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.spec.types import AgentSpec, MCPServerConfig

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

    async def call(value: str) -> str:
        return await manager.call_tool(spec, "connectors__echo", {"value": value})

    try:
        yield call
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_runner_recovers_after_one_upstream_error(tmp_path, status):
    from mcp.shared.exceptions import McpError

    app = _echo_app()
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

    async with (
        _serving(flaky) as sock,
        _runner_manager(tmp_path, _bridge_params(_write_coords(tmp_path, sock))) as call,
        asyncio.timeout(30),
    ):
        assert await call("a") == "a"
        fail_next.append(True)
        with pytest.raises(McpError, match=f"HTTP {status}"):
            await call("b")
        sent = len(posts)
        assert await call("c") == "c"
        assert len(posts) > sent


@pytest.mark.asyncio
@pytest.mark.parametrize("json_response", [False, True])
async def test_runner_recovers_after_a_reset_mid_response(tmp_path, json_response):
    from mcp.shared.exceptions import McpError

    reset_once = (
        "import httpx\n"
        "forward = httpx.AsyncHTTPTransport.handle_async_request\n"
        "class Reset(httpx.AsyncByteStream):\n"
        "    def __init__(self, inner):\n"
        "        self.inner = inner\n"
        "    async def __aiter__(self):\n"
        '        yield b\'event: message\\ndata: {"jsonrpc":"2.0","id":\'\n'
        "        raise httpx.ReadError('connection reset')\n"
        "    async def aclose(self):\n"
        "        await self.inner.aclose()\n"
        "async def reset(self, request):\n"
        "    response = await forward(self, request)\n"
        "    marker = Path(sys.argv[1]).with_name('reset-once')\n"
        "    if b'tools/call' in request.content and not marker.exists():\n"
        "        marker.touch()\n"
        "        response.stream = Reset(response.stream)\n"
        "    return response\n"
        "httpx.AsyncHTTPTransport.handle_async_request = reset\n"
    )
    async with (
        _serving(_echo_app(json_response)) as sock,
        _runner_manager(
            tmp_path, _bridge_params(_write_coords(tmp_path, sock), reset_once)
        ) as call,
        asyncio.timeout(30),
    ):
        if json_response:
            assert await call("b") == "b"
        else:
            with pytest.raises(McpError, match="connection lost"):
                await call("b")
        assert (tmp_path / "reset-once").exists()
        assert await call("c") == "c"


@pytest.mark.asyncio
async def test_transport_answers_connect_errors_with_correlated_rpc_errors():
    from omnigent.host.mcp_identity import _IdentityTransport

    failures = [True, True]

    def upstream(request: httpx.Request) -> httpx.Response:
        if failures:
            failures.pop()
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 8, "result": {}})

    transport = _IdentityTransport()
    transport.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(transport=transport) as client:
        failed = await client.post(
            "http://relay/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
        )
        assert failed.status_code == 200
        assert failed.json()["id"] == 7
        assert failed.json()["error"]["code"] == -32603
        notification = await client.post(
            "http://relay/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notification.status_code == 202
        assert notification.content == b""
        recovered = await client.post(
            "http://relay/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"}
        )
        assert recovered.json() == {"jsonrpc": "2.0", "id": 8, "result": {}}


@pytest.mark.asyncio
async def test_stdio_bridge_exits_when_its_relay_fails(tmp_path):
    from mcp.shared.exceptions import McpError

    crash = (
        "forward = bridge._IdentityTransport.handle_async_request\n"
        "async def crash(self, request):\n"
        "    if request.method == 'POST' and b'tools/call' in request.content:\n"
        "        raise RuntimeError('relay crashed')\n"
        "    return await forward(self, request)\n"
        "bridge._IdentityTransport.handle_async_request = crash\n"
    )
    async with (
        _serving(_echo_app()) as sock,
        asyncio.timeout(30),
        stdio_client(_bridge_params(_write_coords(tmp_path, sock), crash)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        with pytest.raises(McpError, match="Connection closed"):
            await session.call_tool("echo", {"value": "lost"})


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], fail: bool) -> None:
        self.chunks = chunks
        self.fail = fail

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("connection reset")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "fail", "expected"),
    [
        ([b"data: 1\n\nda", b"ta: 2\n\n"], False, b"data: 1\n\ndata: 2\n\n"),
        ([b"data: 1\r\n\r\ntail"], False, b"data: 1\r\n\r\ntail"),
        ([b"data: 1\r\rdata: 2\r\r"], False, b"data: 1\r\rdata: 2\r\r"),
        ([b"data: 1\n\r", b"\ndata: 2\n\n"], False, b"data: 1\n\r\ndata: 2\n\n"),
        (
            [b"event: message\ndata: 1\n\nevent: mes", b'sage\ndata: {"jsonrpc":'],
            True,
            b"event: message\ndata: 1\n\nevent: message\ndata: "
            b'{"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, '
            b'"message": "MCP connector connection lost; retry the call"}}\n\n',
        ),
    ],
)
async def test_reset_stream_passes_only_complete_events(chunks, fail, expected):
    from omnigent.host.mcp_identity import _ErrorOnResetStream

    stream = _ErrorOnResetStream(_Chunks(chunks, fail), 7)
    assert b"".join([chunk async for chunk in stream]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", [b"\n\n", b"\r\n\r\n", b"\r\r", b"\n\r\n", b"\r\n\r"])
async def test_reset_stream_forwards_a_complete_event_before_eof(framing):
    from omnigent.host.mcp_identity import _ErrorOnResetStream

    release = asyncio.Event()

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message\ndata: elicitation" + framing[:1]
            yield framing[1:]
            await release.wait()

        async def aclose(self) -> None:
            return None

    events = _ErrorOnResetStream(Held(), 7).__aiter__()
    first = await asyncio.wait_for(events.__anext__(), timeout=2)
    assert first == b"event: message\ndata: elicitation" + framing
    release.set()
    with pytest.raises(StopAsyncIteration):
        await events.__anext__()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "503"])
async def test_transport_surfaces_a_failed_reply(failure):
    from omnigent.host.mcp_identity import _IdentityTransport

    def upstream(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(503)

    transport = _IdentityTransport()
    transport.transport = httpx.MockTransport(upstream)
    reply = {"jsonrpc": "2.0", "id": 3, "result": {"action": "accept"}}
    async with httpx.AsyncClient(transport=transport) as client:
        if failure == "connect":
            with pytest.raises(httpx.ConnectError):
                await client.post("http://relay/mcp", json=reply)
        else:
            response = await client.post("http://relay/mcp", json=reply)
            assert response.status_code == 503


@pytest.mark.asyncio
async def test_reset_stream_scans_a_large_event_in_linear_time():
    from omnigent.host.mcp_identity import _ErrorOnResetStream

    payload = b"data: " + b"x" * (8 * 2**20) + b"\n\n"
    chunks = [payload[i : i + 4096] for i in range(0, len(payload), 4096)]
    loop = asyncio.get_running_loop()
    started = loop.time()
    received = b"".join([c async for c in _ErrorOnResetStream(_Chunks(chunks, False), 7)])
    assert received == payload
    assert loop.time() - started < 5
