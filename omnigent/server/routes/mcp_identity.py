"""Host-authenticated streaming MCP relay; upstream identity comes from the owner."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import anyio
import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response, StreamingResponse

from omnigent.server.oidc_mcp import DelegationUnavailable, MCPDelegationBroker
from omnigent.stores.host_store import HostStore

_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
}
_RESPONSE_HEADERS = {"content-type", "mcp-session-id", "mcp-protocol-version", "retry-after"}


def create_mcp_identity_router(host_store: HostStore, broker: MCPDelegationBroker) -> APIRouter:
    """Relay only to the configured upstream, using a validated managed-host owner."""
    router = APIRouter()
    session_key = hmac.new(
        broker.oidc.cookie_secret, b"omnigent.mcp-session.v1", hashlib.sha256
    ).digest()

    @router.api_route("/hosts/{host_id}/mcp", methods=["GET", "POST", "DELETE"])
    async def relay(host_id: str, request: Request):
        token = request.headers.get("x-omnigent-host-token", "")
        if not token:
            raise HTTPException(401, "Managed host authentication required")
        host = await asyncio.to_thread(host_store.resolve_launch_token, host_id, token)
        if host is None:
            raise HTTPException(401, "Managed host authentication required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise HTTPException(413, "MCP request too large")
        try:
            access_token = await asyncio.to_thread(broker.credential, host.user_id)
        except DelegationUnavailable as exc:
            raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
        headers = {key: value for key, value in request.headers.items() if key in _REQUEST_HEADERS}
        upstream_session = headers.pop("mcp-session-id", None)
        if upstream_session:
            try:
                binding = jwt.decode(
                    upstream_session,
                    session_key,
                    algorithms=["HS256"],
                    options={"require": ["host", "owner", "upstream"]},
                )
                if binding["host"] != host_id or binding["owner"] != host.user_id:
                    raise ValueError("Wrong session owner")
                if not isinstance(binding["upstream"], str):
                    raise ValueError("Invalid session binding")
                headers["mcp-session-id"] = binding["upstream"]
            except (jwt.PyJWTError, ValueError):
                raise HTTPException(403, "Invalid MCP session binding") from None
        headers["Authorization"] = "Bearer " + access_token
        client = httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=httpx.Timeout(15, read=300)
        )
        try:
            upstream = await client.send(
                client.build_request(
                    request.method,
                    broker.config.upstream_url,
                    headers=headers,
                    content=bytes(body),
                ),
                stream=True,
            )
        except httpx.HTTPError:
            with anyio.CancelScope(shield=True):
                await client.aclose()
            return Response(
                "MCP upstream unavailable", status_code=502, headers={"Cache-Control": "no-store"}
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await client.aclose()
            raise
        response_headers = {
            key: value for key, value in upstream.headers.items() if key in _RESPONSE_HEADERS
        }
        # Host token validity bounds this binding; do not expire an active upstream session.
        if "mcp-session-id" in response_headers:
            response_headers["mcp-session-id"] = jwt.encode(
                {
                    "host": host_id,
                    "owner": host.user_id,
                    "upstream": response_headers["mcp-session-id"],
                },
                session_key,
                algorithm="HS256",
            )
        response_headers["Cache-Control"] = "no-store"

        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await client.aclose()

        return StreamingResponse(
            chunks(), status_code=upstream.status_code, headers=response_headers
        )

    return router
