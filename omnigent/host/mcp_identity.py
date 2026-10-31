"""Stdio MCP bridge to the managed host's owner-authenticated server relay."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from urllib.parse import quote

import anyio
import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from omnigent.host.inference_credential import _coords_path

_UNAVAILABLE = "MCP identity unavailable; sign in again or renew API delegation"
_LINE_END = re.compile(rb"\r\n|\r|\n")


def _rpc_request(request: httpx.Request) -> dict[str, object] | None:
    body = json.loads(request.content)
    if not isinstance(body, dict) or "id" not in body or "method" not in body:
        return None
    return body


def _is_reply(request: httpx.Request) -> bool:
    body = json.loads(request.content)
    return isinstance(body, dict) and "id" in body and "method" not in body


def _rpc_error(request: httpx.Request, error: dict[str, object]) -> httpx.Response:
    body = _rpc_request(request)
    if body is None:
        return httpx.Response(202)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})


class _ErrorOnResetStream(httpx.AsyncByteStream):
    """SSE body that ends with a JSON-RPC error event for ``request_id`` if the transport fails.

    Only complete events are passed on, so a reset never delivers a truncated one.
    """

    def __init__(self, inner: httpx.AsyncByteStream, request_id: object) -> None:
        self.inner = inner
        self.request_id = request_id

    async def __aiter__(self) -> AsyncIterator[bytes]:
        # An event ends at an empty line. A CR ending one chunk and an LF starting the
        # next are one line ending.
        pending = bytearray()
        index = line_start = 0
        split_crlf = False
        try:
            async for chunk in self.inner:
                pending += chunk
                end = 0
                if split_crlf and pending[index : index + 1] == b"\n":
                    index = line_start = index + 1
                split_crlf = False
                for line_end in _LINE_END.finditer(pending, index):
                    if line_end.start() == line_start:
                        end = line_end.end()
                    line_start = line_end.end()
                    split_crlf = line_end.group() == b"\r" and line_end.end() == len(pending)
                index = len(pending)
                if end:
                    yield bytes(pending[:end])
                    del pending[:end]
                    index -= end
                    line_start -= end
        except httpx.TransportError:
            error = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "error": {
                    "code": -32603,
                    "message": "MCP connector connection lost; retry the call",
                },
            }
            yield b"event: message\ndata: " + json.dumps(error).encode() + b"\n\n"
            return
        if pending:
            yield bytes(pending)

    async def aclose(self) -> None:
        await self.inner.aclose()


class _IdentityTransport(httpx.AsyncBaseTransport):
    """Answer a failed POST with a JSON-RPC error the stdio caller can correlate."""

    def __init__(self) -> None:
        self.transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return await self.transport.handle_async_request(request)
        try:
            response = await self.transport.handle_async_request(request)
        except httpx.TransportError:
            # A lost reply to an upstream request must end the relay, not vanish.
            if _is_reply(request):
                raise
            return _rpc_error(
                request,
                {"code": -32603, "message": "MCP connector unreachable; retry the call"},
            )
        status = response.status_code
        # 404 is the SDK's session-terminated signal and is handled there.
        if status < 400 or status == 404:
            body = _rpc_request(request)
            if (
                body is not None
                and response.headers.get("content-type", "").startswith("text/event-stream")
                and isinstance(response.stream, httpx.AsyncByteStream)
            ):
                response.stream = _ErrorOnResetStream(response.stream, body["id"])
            return response
        await response.aclose()
        if _is_reply(request):
            return httpx.Response(status, request=request)
        if status in (401, 403):
            return _rpc_error(
                request,
                {
                    "code": -32001,
                    "message": "Sign in again to restore connector access",
                    "data": {"identity_login_required": True},
                },
            )
        return _rpc_error(
            request,
            {"code": -32603, "message": f"MCP connector request failed (HTTP {status})"},
        )

    async def aclose(self) -> None:
        await self.transport.aclose()


def _identity_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(15, read=300),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
        transport=_IdentityTransport(),
    )


async def main() -> None:
    """Keep IdP tokens out of the runner; use only its existing host launch identity."""
    coords = json.loads(_coords_path().read_text())
    url = f"{coords['server'].rstrip('/')}/v1/hosts/{quote(coords['host_id'], safe='')}/mcp"
    async with stdio_server() as (local_read, local_write):
        caller_closed = anyio.Event()
        # CLI boundary: relay errors must not expose provider or credential details.
        with suppress(Exception):
            async with (
                streamablehttp_client(
                    url,
                    headers={"X-Omnigent-Host-Token": coords["host_token"]},
                    httpx_client_factory=_identity_client,
                ) as (remote_read, remote_write, _),
                anyio.create_task_group() as group,
            ):

                async def copy(source, target):
                    async for message in source:
                        if isinstance(message, Exception):
                            raise RuntimeError("MCP identity relay unavailable") from None
                        await target.send(message)

                async def from_caller():
                    await copy(local_read, remote_write)
                    caller_closed.set()
                    group.cancel_scope.cancel()

                async def to_caller():
                    await copy(remote_read, local_write)
                    group.cancel_scope.cancel()

                group.start_soon(from_caller)
                group.start_soon(to_caller)
        if not caller_closed.is_set():
            # stdio_server cannot cancel its blocking stdin read; exit so the caller sees EOF.
            print(_UNAVAILABLE, file=sys.stderr, flush=True)
            os._exit(1)


if __name__ == "__main__":
    try:
        anyio.run(main)
    except Exception:  # noqa: BLE001 - CLI boundary must not expose provider or credential errors.
        print(_UNAVAILABLE, file=sys.stderr)
        raise SystemExit(1) from None
