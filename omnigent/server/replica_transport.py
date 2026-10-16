"""Authenticated peer forwarding that preserves the original request authority."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from omnigent.db.db_models import current_workspace_id
from omnigent.stores.backend_route_store import BackendRoute
from omnigent.util.tunnel_limits import (
    RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
)

PEER_HEADER = b"x-omnigent-peer"
_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
    PEER_HEADER,
}
_WS_HEADERS = {
    b"host",
    b"sec-websocket-key",
    b"sec-websocket-version",
    b"sec-websocket-extensions",
    b"sec-websocket-protocol",
}


class _PeerConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        """Peer addresses are fixed; redirects must never receive client credentials."""
        return exc


def _headers(raw: list[tuple[bytes, bytes]], *, websocket: bool = False):
    excluded = set(_HOP_HEADERS)
    if websocket:
        excluded.update(_WS_HEADERS)
    for name, value in raw:
        if name.lower() == b"connection":
            excluded.update(part.strip().lower() for part in value.split(b","))
    return [(name, value) for name, value in raw if name.lower() not in excluded]


@dataclass(frozen=True)
class ReplicationConfig:
    endpoint: str
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    key: bytes = field(repr=False)
    lease_seconds: float = 90

    @classmethod
    def parse(cls, raw: object) -> ReplicationConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) - {
            "endpoint_env",
            "peer_key_env",
            "allowed_networks",
            "lease_seconds",
        }:
            raise ValueError("invalid replication configuration")
        endpoint_env = raw.get("endpoint_env", "OMNIGENT_REPLICA_URL")
        key_env = raw.get("peer_key_env", "OMNIGENT_REPLICA_KEY")
        if not isinstance(endpoint_env, str) or not isinstance(key_env, str):
            raise ValueError("replication environment variable names must be strings")
        endpoint = os.environ.get(endpoint_env, "")
        key = os.environ.get(key_env, "").encode()
        if len(key) < 32:
            raise ValueError("replication requires a shared peer key of at least 32 bytes")
        networks = raw.get("allowed_networks")
        if not isinstance(networks, list) or not networks:
            raise ValueError("replication.allowed_networks must list the backend IP ranges")
        parsed = tuple(ipaddress.ip_network(value, strict=True) for value in networks)
        if any(network.prefixlen == 0 for network in parsed):
            raise ValueError("replication peer networks cannot allow every address")
        ttl = raw.get("lease_seconds", 90)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 15 <= ttl <= 300:
            raise ValueError("replication.lease_seconds must be between 15 and 300")
        config = cls(endpoint, parsed, key, float(ttl))
        config.validate_endpoint(endpoint)
        return config

    def validate_endpoint(self, endpoint: str) -> None:
        """Use operator-allowed literal IPs, never client-selected or rebindable hosts."""
        uri = urlsplit(endpoint)
        if (
            uri.scheme not in ("http", "https")
            or uri.username is not None
            or uri.password is not None
            or uri.path not in ("", "/")
            or uri.query
            or uri.fragment
            or uri.hostname is None
            or "%" in uri.netloc
            or any(ord(character) <= 32 for character in endpoint)
        ):
            raise ValueError("invalid replication peer endpoint")
        try:
            address = ipaddress.ip_address(uri.hostname)
            port = uri.port
        except ValueError:
            raise ValueError(
                "replication peer endpoints require a literal IP and valid port"
            ) from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid replication peer port")
        if not any(address in network for network in self.networks):
            raise ValueError("replication peer endpoint is outside the configured networks")


class PeerTransport:
    """Forward once to a known resource owner, with ordinary authentication intact."""

    def __init__(self, config: ReplicationConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(5, read=None, write=None),
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _context(self, scope: Scope, route: BackendRoute) -> str:
        host = next(
            (
                value.decode("latin1")
                for name, value in scope["headers"]
                if name.lower() == b"host"
            ),
            "",
        )
        value = json.dumps(
            {
                "at": int(time.time()),
                "workspace": current_workspace_id(),
                "kind": route.kind,
                "resource": route.resource_id,
                "token": route.token,
                "replica": route.replica_id,
                "path": scope["path"],
                "raw_path": base64.urlsafe_b64encode(scope.get("raw_path", b"")).decode(),
                "query": base64.urlsafe_b64encode(scope.get("query_string", b"")).decode(),
                "method": scope.get("method", "WS"),
                "scheme": scope["scheme"],
                "host": host,
                "client": scope.get("client"),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(value).decode()
        digest = hmac.new(self.config.key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{digest}"

    def verify_context(self, scope: Scope) -> Mapping[str, object] | None:
        """Validate a peer hop before restoring origin information; never skip auth."""
        headers = [value for name, value in scope["headers"] if name.lower() == PEER_HEADER]
        if not headers:
            return None
        try:
            if len(headers) != 1 or len(headers[0]) > 8192:
                raise ValueError
            encoded, digest = headers[0].decode("ascii").rsplit(".", 1)
            expected = hmac.new(self.config.key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(digest, expected):
                raise ValueError
            context = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
            if not isinstance(context, dict):
                raise ValueError
            if (
                not isinstance(context.get("at"), int)
                or abs(time.time() - context["at"]) > 30
                or context.get("path") != scope["path"]
                or context.get("raw_path")
                != base64.urlsafe_b64encode(scope.get("raw_path", b"")).decode()
                or context.get("method") != scope.get("method", "WS")
                or context.get("query")
                != base64.urlsafe_b64encode(scope.get("query_string", b"")).decode()
                or context.get("scheme") not in ("http", "https", "ws", "wss")
                or not isinstance(context.get("host"), str)
                or not isinstance(context.get("workspace"), int)
                or not isinstance(context.get("client"), (list, type(None)))
                or any(
                    not isinstance(context.get(key), str)
                    for key in ("kind", "resource", "token", "replica")
                )
            ):
                raise ValueError
            return context
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ValueError("invalid backend peer context") from None

    @staticmethod
    def restore_origin(scope: Scope, context: Mapping[str, object]) -> Scope:
        restored = dict(scope)
        restored["scheme"] = context["scheme"]
        client = context["client"]
        restored["client"] = tuple(client) if isinstance(client, list) else None
        restored["headers"] = [
            (name, value)
            for name, value in scope["headers"]
            if name.lower() not in (PEER_HEADER, b"host")
        ] + [(b"host", str(context["host"]).encode("latin1"))]
        return restored

    def _url(self, scope: Scope, route: BackendRoute, *, websocket: bool = False) -> str:
        self.config.validate_endpoint(route.endpoint)
        path = scope.get("root_path", "").encode() + scope.get("raw_path", scope["path"].encode())
        query = scope.get("query_string", b"")
        url = httpx.URL(route.endpoint).copy_with(raw_path=path + (b"?" + query if query else b""))
        if websocket:
            url = url.copy_with(scheme="wss" if url.scheme == "https" else "ws")
        return str(url)

    async def forward_http(
        self, scope: Scope, receive: Receive, send: Send, route: BackendRoute
    ) -> None:
        request = Request(scope, receive)
        headers = _headers(scope["headers"])
        headers.append((PEER_HEADER, self._context(scope, route).encode()))
        upstream: httpx.Response | None = None
        try:
            upstream = await self._client.send(
                self._client.build_request(
                    scope["method"],
                    self._url(scope, route),
                    headers=headers,
                    content=request.stream(),
                ),
                stream=True,
            )
            response = StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                background=BackgroundTask(upstream.aclose),
            )
            response.raw_headers = _headers(upstream.headers.raw)
            await response(scope, receive, send)
        except httpx.TransportError:
            if upstream is not None:
                raise
            await JSONResponse(
                {"detail": "Session backend is reconnecting; retry shortly"},
                status_code=503,
                headers={"Retry-After": "1"},
            )(scope, receive, send)
        finally:
            if upstream is not None:
                await upstream.aclose()

    async def runner_inventory(self, scope: Scope, route: BackendRoute) -> list[dict[str, object]]:
        """Query the peer's ordinary, user-authenticated runner-list endpoint."""
        headers = _headers(scope["headers"])
        headers.append((PEER_HEADER, self._context(scope, route).encode()))
        response = await self._client.get(self._url(scope, route), headers=headers, timeout=3)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("invalid backend runner inventory")
        return [
            row
            for row in payload["data"]
            if isinstance(row, dict) and isinstance(row.get("runner_id"), str)
        ]

    async def cleanup_session_worktree(
        self, request: Request, route: BackendRoute, session_id: str
    ) -> None:
        """Ask the host owner to clean the stored worktree with caller authorization."""
        from starlette.exceptions import HTTPException

        path = f"/v1/sessions/{session_id}/resources/worktree"
        scope: Scope = {
            **request.scope,
            "method": "DELETE",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
        }
        # This is a separate bodyless operation, even if the caller supplied
        # an ignored DELETE body on the session endpoint.
        headers = [
            (key, value)
            for key, value in _headers(scope["headers"])
            if key.lower() != b"content-length"
        ]
        headers.append((PEER_HEADER, self._context(scope, route).encode()))
        try:
            response = await self._client.delete(
                self._url(scope, route), headers=headers, timeout=180
            )
        except httpx.TransportError:
            raise HTTPException(503, "Session host is reconnecting; retry shortly") from None
        if response.is_error:
            try:
                detail = response.json().get("detail", "Session worktree cleanup failed")
            except (ValueError, AttributeError):
                detail = "Session worktree cleanup failed"
            raise HTTPException(response.status_code, detail)

    async def forward_websocket(self, ws: WebSocket, route: BackendRoute) -> None:
        headers = _headers(ws.scope["headers"], websocket=True)
        headers.append((PEER_HEADER, self._context(ws.scope, route).encode()))
        accepted = False
        try:
            async with _PeerConnect(
                self._url(ws.scope, route, websocket=True),
                additional_headers=[
                    (key.decode("latin1"), value.decode("latin1")) for key, value in headers
                ],
                subprotocols=ws.scope.get("subprotocols") or None,
                open_timeout=5,
                max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
                ping_interval=TUNNEL_KEEPALIVE_PING_INTERVAL_S,
                ping_timeout=TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
            ) as peer:
                await ws.accept(subprotocol=peer.subprotocol)
                accepted = True

                async def to_peer():
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        text = message.get("text")
                        if isinstance(text, str):
                            await peer.send(text)
                        else:
                            await peer.send(message["bytes"])

                async def to_client():
                    async for message in peer:
                        if isinstance(message, str):
                            await ws.send_text(message)
                        else:
                            await ws.send_bytes(message)

                tasks = [asyncio.create_task(to_peer()), asyncio.create_task(to_client())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                if ws.client_state.name != "DISCONNECTED":
                    code = peer.close_code or 1011
                    await ws.close(
                        code=code if code not in (1005, 1006, 1015) else 1011,
                        reason=peer.close_reason or "",
                    )
        except InvalidStatus as exc:
            if not accepted:
                await ws.send_denial_response(
                    JSONResponse(
                        {"detail": "Session backend unavailable"},
                        status_code=exc.response.status_code,
                    )
                )
        except (OSError, TimeoutError):
            if not accepted:
                await ws.send_denial_response(
                    JSONResponse(
                        {"detail": "Session backend is reconnecting; retry shortly"},
                        status_code=503,
                    )
                )
        except ConnectionClosed as exc:
            if accepted and ws.client_state.name != "DISCONNECTED":
                close = exc.rcvd
                code = close.code if close is not None else 1012
                reason = close.reason if close is not None else ""
                await ws.close(
                    code=code if code not in (1005, 1006, 1015) else 1012, reason=reason
                )
        except WebSocketDisconnect:
            if accepted and ws.client_state.name != "DISCONNECTED":
                await ws.close(code=1012)
