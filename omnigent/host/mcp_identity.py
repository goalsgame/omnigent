"""Stdio MCP bridge to the managed host's owner-authenticated server relay."""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

import anyio
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from omnigent.host.inference_credential import _coords_path


async def main() -> None:
    """Keep IdP tokens out of the runner; use only its existing host launch identity."""
    coords = json.loads(_coords_path().read_text())
    url = f"{coords['server'].rstrip('/')}/v1/hosts/{quote(coords['host_id'], safe='')}/mcp"
    async with (
        stdio_server() as (local_read, local_write),
        streamablehttp_client(
            url,
            headers={"X-Omnigent-Host-Token": coords["host_token"]},
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
