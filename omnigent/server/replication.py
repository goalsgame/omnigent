"""Resource ownership and lifecycle for active backend replicas."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from omnigent.db.backend_fence import backend_fence_scope
from omnigent.db.backend_work_fence import BackendWorkFence, guarded_work
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.replica_transport import PeerTransport, ReplicationConfig
from omnigent.stores.backend_route_store import BackendRoute, BackendRouteStore

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostRegistry
    from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)
_current: ContextVar[ReplicaCoordinator | None] = ContextVar("replica_coordinator", default=None)
_P = ParamSpec("_P")
_T = TypeVar("_T")
_held: ContextVar[tuple[tuple[int, str, str], _OwnedResource] | None] = ContextVar(
    "backend_request_placement", default=None
)


@contextmanager
def replication_scope(coordinator: ReplicaCoordinator | None) -> Iterator[None]:
    handle = _current.set(coordinator)
    try:
        yield
    finally:
        _current.reset(handle)


@asynccontextmanager
async def reserve_managed_placement(host_id: str) -> AsyncIterator[None]:
    """Keep a provisioning host on the replica waiting for its launch to finish."""
    coordinator = _current.get()
    if coordinator is None:
        yield
    else:
        async with coordinator.reserve(host_id):
            yield


def placed_managed_launch(
    function: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Awaitable[_T]]:
    """Reserve the host_id keyword target through registration and startup."""

    @wraps(function)
    async def placed(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        host_id = kwargs.get("host_id")
        if not isinstance(host_id, str):
            raise ValueError("managed placement requires a host_id")
        async with reserve_managed_placement(host_id):
            return await function(*args, **kwargs)

    return placed


def create_managed_launch_task(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    coordinator = _current.get()
    if coordinator is None:
        return asyncio.create_task(coroutine)
    fence = BackendWorkFence(coordinator.database_uri)
    held = _held.get()
    if held is not None:
        key, entry = held
        fence.routes[key] = entry.route
    task = asyncio.create_task(guarded_work(coroutine, fence))
    fences = coordinator._task_fences
    fences[task] = fence

    def completed(done: asyncio.Task[None]) -> None:
        fences.pop(done, None)
        # Cancellation before the wrapper starts must close the supplied coroutine.
        coroutine.close()

    task.add_done_callback(completed)
    return task


def retain_managed_task(task: asyncio.Task[None]) -> None:
    """Transfer a request's placement reference to its background launch task."""
    coordinator = _current.get()
    if coordinator is not None:
        coordinator.retain_task(task)


async def create_session_launch_task(
    session_id: str, coroutine: Coroutine[Any, Any, None]
) -> asyncio.Task[None]:
    """Reserve the new session before scheduling any provider work."""
    coordinator = _current.get()
    if coordinator is None:
        return create_managed_launch_task(coroutine)
    try:
        async with coordinator.request_resource("session", session_id) as route:
            if not coordinator.local(route):
                raise RuntimeError("new session is owned by another backend")
            task = create_managed_launch_task(coroutine)
            coordinator.retain_task(task)
            return task
    except BaseException:
        coroutine.close()
        raise


@dataclass
class _OwnedResource:
    route: BackendRoute
    connections: dict[str, WebSocket | None] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)


@dataclass
class _ResourceLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ReplicaCoordinator:
    def __init__(
        self,
        config: ReplicationConfig,
        database_uri: str,
        conversations: ConversationStore,
        hosts: HostRegistry,
    ) -> None:
        self.config = config
        self.database_uri = database_uri
        self._task_fences: dict[asyncio.Task[None], BackendWorkFence] = {}
        self.store = BackendRouteStore(
            database_uri, uuid.uuid4().hex, config.endpoint, ttl=config.lease_seconds
        )
        self.transport = PeerTransport(config)
        self.conversations = conversations
        self.hosts = hosts
        self._owned: dict[tuple[int, str, str], _OwnedResource] = {}
        self._locks: dict[tuple[int, str, str], _ResourceLock] = {}
        self._task: asyncio.Task[None] | None = None
        self._cleanup: set[asyncio.Task[None]] = set()

    def local(self, route: BackendRoute) -> bool:
        return route.replica_id == self.store.replica_id

    @asynccontextmanager
    async def _resource_lock(self, kind: str, resource_id: str) -> AsyncIterator[None]:
        key = (current_workspace_id(), kind, resource_id)
        entry = self._locks.setdefault(key, _ResourceLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                self._locks.pop(key, None)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._renew_loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        launches = list(self._task_fences.items())
        for task, fence in launches:
            fence.lost = True
            task.cancel("backend is stopping")
        if launches:
            await asyncio.gather(*(task for task, _ in launches), return_exceptions=True)
        if self._cleanup:
            await asyncio.gather(*self._cleanup, return_exceptions=True)
        for owned in list(self._owned.values()):
            with workspace_scope(owned.route.workspace_id):
                try:
                    await asyncio.to_thread(self.store.release, owned.route)
                except Exception:
                    _logger.exception("Could not release backend placement during shutdown")
        self._owned.clear()
        await self.transport.aclose()

    def _runner_host(self, runner_id: str) -> str | None:
        from omnigent.runner.routing import routing_host_id

        for conv in self.conversations.list_conversations_by_runner_id(runner_id):
            if host_id := routing_host_id(conv, self.conversations):
                return host_id
        return None

    async def runner_route(self, runner_id: str) -> BackendRoute | None:
        host_id = await asyncio.to_thread(self._runner_host, runner_id)
        return await asyncio.to_thread(
            self.store.get, "host" if host_id else "runner", host_id or runner_id
        )

    async def remote_runners(self, request: Request) -> list[dict[str, object]]:
        if request.scope.get("omnigent.peer"):
            return []
        peers = await asyncio.to_thread(self.store.peer_routes)
        if len(peers) > 32:
            raise HTTPException(503, "Too many backend peers")

        async def inventory(route: BackendRoute) -> list[dict[str, object]]:
            try:
                return await self.transport.runner_inventory(request.scope, route)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    raise HTTPException(
                        exc.response.status_code, "Authentication rejected"
                    ) from None
                return []
            except (httpx.HTTPError, ValueError):
                return []

        lists = await asyncio.gather(*(inventory(peer) for peer in peers))
        return [item for items in lists for item in items]

    def _host_report(self, host_id: str) -> dict[str, object]:
        conn = self.hosts.get(host_id)
        if conn is None:
            return {}
        return {
            "version": conn.hello.version,
            "gateway_inference": self.hosts.gateway_inference(host_id),
            "interactive_shells": self.hosts.interactive_shells(host_id),
        }

    async def report_host(self, host_id: str) -> None:
        entry = self._owned.get((current_workspace_id(), "host", host_id))
        if entry is not None:
            await asyncio.to_thread(self.store.renew, entry.route, self._host_report(host_id))

    @asynccontextmanager
    async def _reference(self, route: BackendRoute, ws: WebSocket | None) -> AsyncIterator[None]:
        key = (route.workspace_id, route.kind, route.resource_id)
        reference = uuid.uuid4().hex
        entry = self._owned.get(key)
        if entry is None:
            entry = _OwnedResource(route)
            self._owned[key] = entry
        elif entry.route.token != route.token:
            # Claiming a new connection generation also retires every old socket,
            # even if the replacement later fails its protocol handshake.
            entry.route = route
            for task in entry.tasks.values():
                fence = self._task_fences.get(task)
                if fence is not None:
                    fence.routes[key] = route
            for previous in list(entry.connections.values()):
                if previous is not None:
                    with suppress(Exception):
                        await previous.close(code=1012, reason="backend connection replaced")
        elif entry.route.expires_at < route.expires_at:
            entry.route = route
        entry.connections[reference] = ws
        handle = _held.set((key, entry))
        try:
            yield
        finally:
            _held.reset(handle)
            await self._drop_reference(key, entry, reference)

    async def _drop_reference(
        self, key: tuple[int, str, str], entry: _OwnedResource, reference: str
    ) -> None:
        with workspace_scope(key[0]):
            async with self._resource_lock(key[1], key[2]):
                # Lease loss removes the entry before closing its sockets.
                if self._owned.get(key) is not entry:
                    return
                entry.connections.pop(reference, None)
                entry.tasks.pop(reference, None)
                if entry.connections:
                    return
                self._owned.pop(key, None)
                try:
                    await asyncio.to_thread(self.store.release, entry.route)
                except Exception:
                    _logger.exception("Could not release backend resource placement")

    def retain_task(self, task: asyncio.Task[None]) -> None:
        held = _held.get()
        if held is None:
            return
        key, entry = held
        if self._owned.get(key) is not entry:
            return
        reference = uuid.uuid4().hex
        entry.connections[reference] = None
        entry.tasks[reference] = task
        fence = self._task_fences.get(task)
        if fence is not None:
            fence.routes[key] = entry.route

        def completed(_task: asyncio.Task[None]) -> None:
            cleanup = asyncio.create_task(self._drop_reference(key, entry, reference))
            self._cleanup.add(cleanup)
            cleanup.add_done_callback(self._cleanup.discard)

        task.add_done_callback(completed)

    @asynccontextmanager
    async def request_resource(self, kind: str, resource_id: str) -> AsyncIterator[BackendRoute]:
        """Retain placement while an authorized action is dispatched."""
        async with self._resource_lock(kind, resource_id):
            route = await asyncio.to_thread(self.store.get, kind, resource_id)
            if route is None:
                route = await asyncio.to_thread(self.store.claim, kind, resource_id)
            reference = None
            if self.local(route):
                reference = self._reference(route, None)
                await reference.__aenter__()
        try:
            yield route
        finally:
            if reference is not None:
                await reference.__aexit__(None, None, None)

    @asynccontextmanager
    async def request_placement(self, host_id: str) -> AsyncIterator[BackendRoute]:
        async with self.request_resource("host", host_id) as route:
            yield route

    async def release_created(self, routes: list[BackendRoute]) -> None:
        for route in routes:
            with workspace_scope(route.workspace_id):
                async with self._resource_lock(route.kind, route.resource_id):
                    key = (route.workspace_id, route.kind, route.resource_id)
                    if key not in self._owned:
                        await asyncio.to_thread(self.store.release, route)

    @asynccontextmanager
    async def reserve(self, host_id: str) -> AsyncIterator[None]:
        async with self.request_placement(host_id) as route:
            if not self.local(route):
                raise RuntimeError("managed host is owned by another backend")
            yield

    @asynccontextmanager
    async def connection(self, ws: WebSocket, kind: str, resource_id: str) -> AsyncIterator[bool]:
        """Called only after normal tunnel authentication has succeeded."""
        host_id = (
            await asyncio.to_thread(self._runner_host, resource_id) if kind == "runner" else None
        )
        async with self._resource_lock("host" if host_id else kind, host_id or resource_id):
            if host_id:
                route = await asyncio.to_thread(self.store.get, "host", host_id)
            else:
                key = (current_workspace_id(), kind, resource_id)
                entry = self._owned.get(key)
                # The first tunnel of a provisioning host adopts its launch
                # reservation; no prior connection generation needs replacing.
                reserved = entry is not None and not any(entry.connections.values())
                route = (
                    await asyncio.to_thread(self.store.get, kind, resource_id)
                    if reserved
                    else None
                )
                if route is None or not self.local(route):
                    route = await asyncio.to_thread(self.store.claim, kind, resource_id)
            reference = None
            if route is not None and self.local(route):
                # Runners must wait for their host's control tunnel to register.
                if not host_id or self.hosts.get(host_id) is not None:
                    reference = self._reference(route, ws)
                    await reference.__aenter__()
        if reference is None:
            if route is not None and not self.local(route) and not ws.scope.get("omnigent.peer"):
                await self.transport.forward_websocket(ws, route)
            else:
                await ws.send_denial_response(
                    JSONResponse(
                        {"detail": "Session backend is reconnecting; retry shortly"},
                        status_code=503,
                    )
                )
            yield False
            return
        try:
            assert route is not None
            if kind == "host":
                with backend_fence_scope(resource_id, route.token):
                    yield True
            else:
                yield True
        finally:
            await reference.__aexit__(None, None, None)

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.lease_seconds / 3)
            await self._renew_once()

    async def _renew_once(self) -> None:
        for key, entry in list(self._owned.items()):
            with workspace_scope(key[0]):
                current = entry.route
                started = time.time()
                try:
                    report = self._host_report(key[2]) if key[1] == "host" else None
                    renewed = await asyncio.to_thread(self.store.renew, current, report)
                except Exception:
                    _logger.exception("Backend ownership heartbeat failed")
                    if entry.route.expires_at > time.time():
                        continue
                    renewed = False
                if self._owned.get(key) is not entry or entry.route.token != current.token:
                    continue
                if renewed:
                    entry.route = replace(current, expires_at=started + self.store.ttl)
                    continue
                self._owned.pop(key, None)
                for task in set(entry.tasks.values()):
                    fence = self._task_fences.get(task)
                    if fence is not None:
                        fence.lost = True
                    task.cancel("backend ownership changed")
                for ws in list(entry.connections.values()):
                    if ws is not None:
                        with suppress(Exception):
                            await ws.close(code=1012, reason="backend ownership changed")
