"""Route connection-dependent requests to their live backend owner."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from tempfile import SpooledTemporaryFile
from typing import TYPE_CHECKING

from anyio import to_thread
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from omnigent.db.backend_placement import creation_placement_scope
from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes, workspace_scope
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.replication import ReplicaCoordinator, replication_scope
from omnigent.stores.backend_route_store import BackendRoute

if TYPE_CHECKING:
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)
_RESOURCE = re.compile(r"^/v1/(hosts|runners|sessions)/([^/]+)(?:/|$)")


class ReplicaRoutingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        coordinator: ReplicaCoordinator,
        auth_provider: AuthProvider | None,
        host_store: HostStore | None = None,
        permission_store: PermissionStore | None = None,
    ) -> None:
        self.app = app
        self.coordinator = coordinator
        self.auth = auth_provider
        self.host_store = host_store
        self.permissions = permission_store

    def _authorized_mutation_resource(
        self, scope: Scope, kind: str, resource: str
    ) -> tuple[str, str] | None:
        from omnigent.runner.routing import routing_host_id
        from omnigent.server.auth import LEVEL_EDIT
        from omnigent.server.permissions import check_session_access

        try:
            user = self.auth.get_user_id(HTTPConnection(scope)) if self.auth else None
        except OmnigentError:
            return None
        if self.auth is not None and user is None:
            return None
        if kind == "hosts" and self.host_store is not None:
            try:
                host_id = uuid_to_bytes(resource).hex()
            except InvalidUuidError:
                return None
            host = self.host_store.get_host(host_id)
            return (
                ("host", host_id)
                if host is not None and (user is None or host.user_id == user)
                else None
            )
        if kind != "sessions":
            return None
        try:
            session_id = uuid_to_bytes(resource).hex()
        except InvalidUuidError:
            return None
        conv = self.coordinator.conversations.get_conversation(session_id)
        if conv is None:
            return None
        if self.auth is not None:
            if self.permissions is not None:
                if not check_session_access(
                    user,
                    session_id,
                    LEVEL_EDIT,
                    self.permissions,
                    self.coordinator.conversations,
                    conversation=conv,
                ):
                    return None
            elif self.coordinator.conversations.get_session_owner(session_id) != user:
                return None
        host = routing_host_id(conv, self.coordinator.conversations)
        return ("host", host) if host else ("session", session_id)

    async def _error(self, scope: Scope, receive: Receive, send: Send, status: int) -> None:
        response = JSONResponse(
            {
                "detail": "Invalid backend routing context"
                if status == 400
                else "Session backend is reconnecting; retry shortly"
            },
            status_code=status,
            headers={"Retry-After": "1"} if status == 503 else {},
        )
        if scope["type"] == "websocket":
            await WebSocket(scope, receive, send).send_denial_response(response)
        else:
            await response(scope, receive, send)

    async def _session_route(self, session_id: str) -> BackendRoute | None:
        from omnigent.runner.routing import routing_host_id

        try:
            canonical = uuid_to_bytes(session_id).hex()
        except InvalidUuidError:
            return None
        initial = await asyncio.to_thread(self.coordinator.store.get, "session", canonical)
        if initial is not None:
            return initial
        conv = await asyncio.to_thread(self.coordinator.conversations.get_conversation, canonical)
        if conv is None:
            return None
        host = await asyncio.to_thread(routing_host_id, conv, self.coordinator.conversations)
        if host:
            return await asyncio.to_thread(self.coordinator.store.get, "host", host)
        return await self.coordinator.runner_route(conv.runner_id) if conv.runner_id else None

    async def _target(self, kind: str, resource_id: str) -> BackendRoute | None:
        if kind == "sessions":
            return await self._session_route(resource_id)
        if kind == "runners":
            return await self.coordinator.runner_route(resource_id)
        try:
            host = uuid_to_bytes(resource_id).hex()
        except InvalidUuidError:
            return None
        return await asyncio.to_thread(self.coordinator.store.get, "host", host)

    async def _create_target(
        self, scope: Scope, receive: Receive, stack: AsyncExitStack
    ) -> tuple[BackendRoute | None, Receive]:
        # Authenticate before pre-reading uploads. The destination still runs
        # the ordinary route authorization, including scoped-token checks.
        if self.auth is not None:
            try:
                if await asyncio.to_thread(self.auth.get_user_id, HTTPConnection(scope)) is None:
                    return None, receive
            except OmnigentError:
                return None, receive
        spool = stack.enter_context(
            SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115 -- request stack owns the spool
        )

        captured_complete = False

        async def capture():
            nonlocal captured_complete
            message = await receive()
            if message["type"] == "http.request":
                await to_thread.run_sync(spool.write, message.get("body", b""))
                captured_complete = not message.get("more_body", False)
            return message

        request = Request(scope, capture)
        content_type = request.headers.get("content-type", "")
        payload = None
        try:
            if content_type.startswith("application/json"):
                payload = await request.json()
            elif content_type.startswith("multipart/form-data"):
                async with request.form() as form:
                    metadata = form.get("metadata")
                    if isinstance(metadata, str):
                        payload = json.loads(metadata)
            else:
                return None, receive
        except (ValueError, UnicodeError, HTTPException, MultiPartException):
            # Preserve the normal endpoint's error shape for invalid metadata.
            pass
        await to_thread.run_sync(lambda: spool.seek(0))
        replay_complete = False

        async def replay():
            nonlocal replay_complete
            if not replay_complete:
                data = await to_thread.run_sync(spool.read, 65536)
                if data:
                    return {"type": "http.request", "body": data, "more_body": True}
                replay_complete = True
                if captured_complete:
                    return {"type": "http.request", "body": b"", "more_body": False}
            return await receive()

        route = None
        if isinstance(payload, dict):
            for field, kind in (
                ("host_id", "hosts"),
                ("runner_id", "runners"),
                ("parent_session_id", "sessions"),
            ):
                resource_id = payload.get(field)
                if isinstance(resource_id, str) and resource_id:
                    route = await self._target(kind, resource_id)
                    break
        return route, replay

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with replication_scope(self.coordinator):
            if scope["type"] not in ("http", "websocket"):
                await self.app(scope, receive, send)
                return
            try:
                context = self.coordinator.transport.verify_context(scope)
            except ValueError:
                await self._error(scope, receive, send, 400)
                return
            if context is not None:
                workspace = context["workspace"]
                kind, resource = context["kind"], context["resource"]
                assert (
                    isinstance(workspace, int)
                    and isinstance(kind, str)
                    and isinstance(resource, str)
                )
                with workspace_scope(workspace):
                    route = await asyncio.to_thread(self.coordinator.store.get, kind, resource)
                    if (
                        route is None
                        or not self.coordinator.local(route)
                        or route.token != context["token"]
                        or route.replica_id != context["replica"]
                    ):
                        await self._error(scope, receive, send, 503)
                        return
                    scope = self.coordinator.transport.restore_origin(scope, context)
                    scope["omnigent.peer"] = True
                    await self._dispatch(scope, receive, send)
                return
            await self._dispatch(scope, receive, send)

    async def _local(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        store = self.coordinator.store
        with creation_placement_scope(
            store.replica_id, store.endpoint, self.coordinator.config.lease_seconds
        ) as created:
            try:
                await self.app(scope, receive, send)
            finally:
                await self.coordinator.release_created(created)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"]
        # Tunnel handlers authenticate before claiming or forwarding placement.
        if scope["type"] == "websocket" and path.endswith("/tunnel"):
            await self.app(scope, receive, send)
            return
        async with AsyncExitStack() as stack:
            try:
                route = None
                if scope["type"] == "http" and (
                    (
                        scope["method"] == "POST"
                        and (
                            path == "/v1/sessions"
                            or path.endswith("/fork")
                            or path in {"/v1/imports/local", "/v1/imports/local/stream"}
                        )
                    )
                    or (scope["method"] == "PATCH" and re.fullmatch(r"/v1/sessions/[^/]+", path))
                ):
                    route, receive = await self._create_target(scope, receive, stack)
                if route is None and scope["type"] == "http" and path == "/v1/skills":
                    query = Request(scope).query_params
                    if query.get("session_id"):
                        route = await self._target("sessions", query["session_id"])
                    elif query.get("host_id"):
                        route = await self._target("hosts", query["host_id"])
                match = _RESOURCE.match(path)
                if route is None and match:
                    route = await self._target(match.group(1), match.group(2))
                if (
                    match
                    and scope["type"] == "http"
                    and scope["method"] in {"POST", "PATCH", "PUT", "DELETE"}
                    and (route is None or self.coordinator.local(route))
                ):
                    resource = await asyncio.to_thread(
                        self._authorized_mutation_resource, scope, match.group(1), match.group(2)
                    )
                    if resource is not None:
                        route = await stack.enter_async_context(
                            self.coordinator.request_resource(*resource)
                        )
            except Exception:
                _logger.exception("Unable to resolve backend resource placement")
                await self._error(scope, receive, send, 503)
                return
            if route is not None and not self.coordinator.local(route):
                if scope.get("omnigent.peer"):
                    await self._error(scope, receive, send, 503)
                    return
                if scope["type"] == "websocket":
                    await self.coordinator.transport.forward_websocket(
                        WebSocket(scope, receive, send), route
                    )
                else:
                    await self.coordinator.transport.forward_http(scope, receive, send, route)
            else:
                await self._local(scope, receive, send)
