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

from omnigent.db.backend_fence import backend_fence_scope, current_backend_fence
from omnigent.db.backend_work_fence import (
    BackendWorkFence,
    check_backend_work,
    current_work_fence,
    guarded_work,
    work_fence_scope,
)
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.replica_transport import PeerTransport, ReplicationConfig
from omnigent.stores.backend_route_store import BackendRoute, BackendRouteStore

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.managed_hosts import ManagedLaunchTracker
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


async def cleanup_remote_session_worktree(request: Request, session_id: str, host_id: str) -> bool:
    """Delegate only host-local cleanup; the caller still owns runner/session deletion."""
    coordinator = _current.get()
    if coordinator is None:
        return False
    route = await asyncio.to_thread(coordinator.store.get, "host", host_id)
    if route is None or coordinator.local(route):
        return False
    await asyncio.to_thread(check_backend_work)
    await coordinator.transport.cleanup_session_worktree(request, route, session_id)
    await asyncio.to_thread(check_backend_work)
    return True


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


def managed_launch_cancellation(
    tracker: ManagedLaunchTracker, session_id: str
) -> Callable[[], None]:
    entry = tracker.get(session_id)

    def interrupted() -> None:
        if entry is None or tracker.get(session_id) is not entry or entry.settled.is_set():
            return
        reason = "Managed sandbox launch interrupted; retry the session"
        tracker.fail(session_id, reason)
        from omnigent.server.routes._sessions.helpers import _publish_sandbox_status

        _publish_sandbox_status(session_id, "failed", error=reason)

    return interrupted


def create_managed_launch_task(
    coroutine: Coroutine[Any, Any, None], *, on_cancel: Callable[[], None] | None = None
) -> asyncio.Task[None]:
    coordinator = _current.get()
    if coordinator is None:
        task = asyncio.create_task(coroutine)
        fences = None
    else:
        fence = BackendWorkFence(coordinator.database_uri)
        held = _held.get()
        if held is not None:
            key, entry = held
            fence.routes[key] = entry.route
        task = asyncio.create_task(guarded_work(coroutine, fence))
        fences = coordinator._task_fences
        fences[task] = fence

    def completed(done: asyncio.Task[None]) -> None:
        if fences is not None:
            fences.pop(done, None)
        # Cancellation before the wrapper starts must close the supplied coroutine.
        coroutine.close()
        if done.cancelled() and on_cancel is not None:
            on_cancel()

    task.add_done_callback(completed)
    return task


def retain_managed_task(task: asyncio.Task[None]) -> None:
    """Transfer a request's placement reference to its background launch task."""
    coordinator = _current.get()
    if coordinator is not None:
        coordinator.retain_task(task)


async def create_session_launch_task(
    session_id: str,
    coroutine: Coroutine[Any, Any, None],
    *,
    on_cancel: Callable[[], None] | None = None,
) -> asyncio.Task[None]:
    """Reserve the new session before scheduling any provider work."""
    coordinator = _current.get()
    if coordinator is None:
        return create_managed_launch_task(coroutine, on_cancel=on_cancel)
    try:
        async with coordinator.request_resource("session", session_id) as route:
            if not coordinator.local(route):
                raise RuntimeError("new session is owned by another backend")
            task = create_managed_launch_task(coroutine, on_cancel=on_cancel)
            coordinator.retain_task(task)
            return task
    except BaseException:
        coroutine.close()
        if on_cancel is not None:
            on_cancel()
        raise


def create_connection_task(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Retain admitted ownership while accepted connection work drains."""
    task = create_managed_launch_task(coroutine)
    retain_managed_task(task)
    return task


@dataclass(frozen=True)
class _RunnerAdmission:
    route: BackendRoute
    nonce: str


@dataclass
class _OwnedResource:
    route: BackendRoute
    connections: dict[str, WebSocket | None] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    retained_tasks: set[asyncio.Task[None]] = field(default_factory=set)


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
        if (
            conversations.storage_location != database_uri
            or (conversations.conversation_storage_location or database_uri) != database_uri
        ):
            raise ValueError(
                "replication requires one shared metadata and conversation database URI"
            )
        self.config = config
        self.database_uri = database_uri
        self._task_fences: dict[asyncio.Task[None], BackendWorkFence] = {}
        self._runner_connections: dict[tuple[int, str], tuple[str, WebSocket]] = {}
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

        if self.store.is_standalone_runner(runner_id):
            return None
        for conv in self.conversations.list_conversations_by_runner_id(runner_id):
            if host_id := routing_host_id(conv, self.conversations):
                return host_id
        return None

    async def runner_route(self, runner_id: str) -> BackendRoute | None:
        direct = await asyncio.to_thread(self.store.get, "runner", runner_id)
        if direct is not None:
            return direct
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

    @asynccontextmanager
    async def host_registration(self, host_id: str) -> AsyncIterator[bool]:
        """Serialize registry insertion with local socket replacement."""
        async with self._resource_lock("host", host_id):
            fence = current_backend_fence()
            route = await asyncio.to_thread(self.store.get, "host", host_id)
            entry = self._owned.get((current_workspace_id(), "host", host_id))
            yield (
                fence is not None
                and fence.workspace_id == current_workspace_id()
                and fence.host_id == host_id
                and route is not None
                and self.local(route)
                and (route.connection_token or route.token) == fence.token
                and entry is not None
                and entry.route.token == route.token
                and entry.route.connection_token == route.connection_token
            )

    @asynccontextmanager
    async def runner_registration(self, ws: WebSocket, runner_id: str) -> AsyncIterator[bool]:
        """Serialize runner registry insertion with its admitted socket generation."""
        admission = ws.scope.get("omnigent.runner_admission")
        if not isinstance(admission, _RunnerAdmission):
            yield False
            return
        admitted = admission.route
        async with self._resource_lock(admitted.kind, admitted.resource_id):
            route = await asyncio.to_thread(self.store.get, admitted.kind, admitted.resource_id)
            current = self._runner_connections.get((admitted.workspace_id, runner_id))
            entry = self._owned.get((admitted.workspace_id, admitted.kind, admitted.resource_id))
            yield (
                current is not None
                and current[0] == admission.nonce
                and current[1] is ws
                and route is not None
                and self.local(route)
                and route.token == admitted.token
                and route.connection_token == admitted.connection_token
                and entry is not None
                and entry.route.token == admitted.token
                and any(socket is ws for socket in entry.connections.values())
            )

    async def report_host(self, host_id: str) -> None:
        entry = self._owned.get((current_workspace_id(), "host", host_id))
        if entry is not None:
            await asyncio.to_thread(self.store.renew, entry.route, self._host_report(host_id))

    @asynccontextmanager
    async def _reference(
        self, route: BackendRoute, ws: WebSocket | None, task: asyncio.Task | None = None
    ) -> AsyncIterator[None]:
        key = (route.workspace_id, route.kind, route.resource_id)
        reference = uuid.uuid4().hex
        entry = self._owned.get(key)
        if entry is not None and entry.route.token != route.token:
            await self._retire_owned(key, entry)
            entry = None
        if entry is None:
            entry = _OwnedResource(route)
            self._owned[key] = entry
        elif entry.route.connection_token != route.connection_token:
            # Claiming a new connection generation also retires every old socket,
            # even if the replacement later fails its protocol handshake.
            entry.route = route
            for previous in list(entry.connections.values()):
                if previous is not None:
                    with suppress(Exception):
                        await previous.close(code=1001, reason="backend connection replaced")
        elif entry.route.expires_at < route.expires_at:
            entry.route = route
        entry.connections[reference] = ws
        if task is not None:
            entry.tasks[reference] = task
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
                    # Lease cleanup is already generation-conditional and must
                    # still run when the guarded launch has been canceled.
                    with work_fence_scope(None):
                        await asyncio.to_thread(self.store.release, entry.route)
                except Exception:
                    _logger.exception("Could not release backend resource placement")

    def retain_task(self, task: asyncio.Task[None]) -> None:
        held = _held.get()
        if held is None:
            return
        key, entry = held
        if self._owned.get(key) is not entry or task in entry.retained_tasks:
            return
        reference = uuid.uuid4().hex
        entry.connections[reference] = None
        entry.tasks[reference] = task
        entry.retained_tasks.add(task)
        fence = self._task_fences.get(task)
        while fence is not None:
            # Retention survives nested scope exit, including its parent work.
            # Never replace an older generation to revive work after lease loss.
            previous = fence.routes.setdefault(key, entry.route)
            if previous.token != entry.route.token:
                fence.lost = True
            fence = fence.parent

        def completed(_task: asyncio.Task[None]) -> None:
            entry.retained_tasks.discard(task)
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
                reference = self._reference(route, None, asyncio.current_task())
                await reference.__aenter__()
        if reference is None:
            yield route
            return
        task = asyncio.current_task()
        previous = self._task_fences.get(task) if task is not None else None
        fence = BackendWorkFence(self.database_uri, parent=current_work_fence())
        fence.routes[(route.workspace_id, route.kind, route.resource_id)] = route
        if task is not None:
            self._task_fences[task] = fence
        try:
            with work_fence_scope(fence):
                yield route
        finally:
            if task is not None:
                if previous is None:
                    self._task_fences.pop(task, None)
                else:
                    self._task_fences[task] = previous
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
            task = asyncio.current_task()
            if task is not None and task in self._task_fences:
                self.retain_task(task)
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
                route, _continuous = await asyncio.to_thread(
                    self.store.claim_connection,
                    kind,
                    resource_id,
                    entry.route if entry is not None else None,
                )
            reference = None
            if route is not None and self.local(route):
                # Runners must wait for their host's control tunnel to register.
                if not host_id or self.hosts.get(host_id) is not None:
                    reference = self._reference(route, ws)
                    await reference.__aenter__()
                    if kind == "runner":
                        admission = _RunnerAdmission(route, uuid.uuid4().hex)
                        key = (route.workspace_id, resource_id)
                        previous = self._runner_connections.get(key)
                        self._runner_connections[key] = (admission.nonce, ws)
                        ws.scope["omnigent.runner_admission"] = admission
                        if previous is not None and previous[1] is not ws:
                            with suppress(Exception):
                                await previous[1].close(
                                    code=1001, reason="backend connection replaced"
                                )
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
            with replication_scope(self):
                if kind == "host":
                    with backend_fence_scope(resource_id, route.connection_token or route.token):
                        yield True
                else:
                    # Accepted ingestion gets an independent retained work fence;
                    # disconnect reconciliation keeps its normal task context.
                    yield True
        finally:
            if kind == "runner":
                key = (route.workspace_id, resource_id)
                current = self._runner_connections.get(key)
                if current is not None and current[1] is ws:
                    self._runner_connections.pop(key, None)
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
                    entry.route = replace(
                        entry.route,
                        expires_at=max(entry.route.expires_at, started + self.store.ttl),
                    )
                    continue
                await self._retire_owned(key, entry)

    async def _retire_owned(self, key: tuple[int, str, str], entry: _OwnedResource) -> None:
        if self._owned.get(key) is entry:
            self._owned.pop(key, None)
        for task in set(entry.tasks.values()):
            fence = self._task_fences.get(task)
            if fence is not None:
                fence.lost = True
            task.cancel("backend ownership changed")
        for ws in list(entry.connections.values()):
            if ws is not None:
                with suppress(Exception):
                    await ws.close(code=1001, reason="backend ownership changed")
