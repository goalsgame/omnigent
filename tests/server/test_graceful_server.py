"""Shared graceful shutdown for the omnigent server launchers."""

import asyncio

import httpx
import pytest
import uvicorn

from omnigent.server import graceful_server


def test_timeout_defaults_to_five_seconds_and_honours_the_override(monkeypatch):
    monkeypatch.delenv("OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S", raising=False)
    assert graceful_server.graceful_shutdown_timeout_s() == 5
    monkeypatch.setenv("OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S", "12")
    assert graceful_server.graceful_shutdown_timeout_s() == 12


async def test_shutdown_is_marked_and_streams_drained_before_uvicorn_waits(monkeypatch):
    from omnigent.runtime import session_stream
    from omnigent.server import shutdown_state

    order = []
    monkeypatch.setattr(shutdown_state, "mark_server_shutting_down", lambda: order.append("mark"))
    monkeypatch.setattr(session_stream, "shutdown_all", lambda: order.append("drain"))

    async def uvicorn_shutdown(self, sockets=None):
        order.append("uvicorn")

    monkeypatch.setattr(uvicorn.server.Server, "shutdown", uvicorn_shutdown)
    server = graceful_server.shutdown_signaling_server(uvicorn.Config(app=lambda *_: None))
    await server.shutdown()
    assert order == ["mark", "drain", "uvicorn"]


async def _endless_stream(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    while True:
        await send({"type": "http.response.body", "body": b"tick\n", "more_body": True})
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("timeout", [1])
async def test_open_stream_cannot_hold_shutdown_past_the_timeout(
    unused_tcp_port, timeout, monkeypatch
):
    from omnigent.server import shutdown_state

    monkeypatch.setattr(shutdown_state, "_marked_at", None)
    config = uvicorn.Config(
        _endless_stream,
        host="127.0.0.1",
        port=unused_tcp_port,
        log_config=None,
        lifespan="off",
        timeout_graceful_shutdown=timeout,
    )
    server = graceful_server.shutdown_signaling_server(config)
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", f"http://127.0.0.1:{unused_tcp_port}/") as response:
            await response.aiter_bytes().__anext__()
            server.should_exit = True
            await asyncio.wait_for(serving, timeout + 4)
