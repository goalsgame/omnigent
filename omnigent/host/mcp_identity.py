"""Stdio MCP bridge to the managed host's owner-authenticated server relay."""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from urllib.parse import quote

import anyio
import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from omnigent.host.inference_credential import _coords_path

_UNAVAILABLE = "MCP identity unavailable; sign in again or renew API delegation"


def _rpc_error(request: httpx.Request, error: dict[str, object]) -> httpx.Response:
    body = json.loads(request.content)
    if not isinstance(body, dict) or "id" not in body or "method" not in body:
        return httpx.Response(202)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})


class _IdentityTransport(httpx.AsyncBaseTransport):
    """Answer a failed POST with a JSON-RPC error the stdio caller can correlate."""

    def __init__(self) -> None:
        self.transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return await self.transport.handle_async_request(request)
        try:
            response = await self.transport.handle_async_request(request)
        except httpx.TransportError:
            return _rpc_error(
                request,
                {"code": -32603, "message": "MCP connector unreachable; retry the call"},
            )
        status = response.status_code
        # 404 is the SDK's session-terminated signal and is handled there.
        if status < 400 or status == 404:
            return response
        await response.aclose()
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
