"""Stdio MCP bridge to the managed host's owner-authenticated server relay."""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

import anyio
import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from omnigent.host.inference_credential import _coords_path


class _IdentityTransport(httpx.AsyncBaseTransport):
    """Translate HTTP rejection into a response the stdio caller can correlate."""

    def __init__(self) -> None:
        self.transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        if response.status_code not in (401, 403) or request.method != "POST":
            return response
        await response.aclose()
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {
                    "code": -32001,
                    "message": "Sign in again to restore connector access",
                    "data": {"identity_login_required": True},
                },
            },
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
    async with (
        stdio_server() as (local_read, local_write),
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
            group.cancel_scope.cancel()

        group.start_soon(copy, local_read, remote_write)
        group.start_soon(copy, remote_read, local_write)


if __name__ == "__main__":
    try:
        anyio.run(main)
    except Exception:  # noqa: BLE001 - CLI boundary must not expose provider or credential errors.
        print("MCP identity unavailable; sign in again or renew API delegation", file=sys.stderr)
        raise SystemExit(1) from None
