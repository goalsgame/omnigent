"""Placement is retained until background launch tasks settle."""

import asyncio
from ipaddress import ip_network

import pytest

from omnigent.server.host_registry import HostRegistry
from omnigent.server.replica_transport import ReplicationConfig
from omnigent.server.replication import ReplicaCoordinator, replication_scope, retain_managed_task
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(autouse=True)
def supported_database(db_uri):
    from sqlalchemy.engine import make_url

    if make_url(db_uri).get_backend_name() not in {"sqlite", "postgresql"}:
        pytest.skip("Replica placement supports SQLite and PostgreSQL")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_request_reference_transfers_to_background_task(db_uri, cancel):
    a = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    b = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8002", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    gate = asyncio.Event()
    task = None
    try:
        with replication_scope(a):
            async with a.request_placement("host") as route:
                task = asyncio.create_task(gate.wait())
                retain_managed_task(task)
                assert a.local(route)
        # The request finished before the background task even had a chance
        # to start; another replica must still forward the wake to A.
        assert b.store.claim("host", "host").replica_id == a.store.replica_id
        if cancel:
            task.cancel()
        else:
            gate.set()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(100):
            if a.store.get("host", "host") is None:
                break
            await asyncio.sleep(0.01)
        assert a.store.get("host", "host") is None
        assert b.store.claim("host", "host").replica_id == b.store.replica_id
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
async def test_nested_request_reservation_does_not_fence_a_live_host(db_uri):
    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    try:
        async with coordinator.request_placement("host") as first:
            async with coordinator.request_placement("host") as second:
                assert first.token == second.token
            assert coordinator.store.get("host", "host").token == first.token
        assert coordinator.store.get("host", "host") is None
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_initial_session_placement_survives_response_until_launch_finishes(db_uri):
    from omnigent.db.backend_placement import creation_placement_scope
    from omnigent.server.replication import create_session_launch_task

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    gate = asyncio.Event()
    task = None
    try:
        with (
            replication_scope(coordinator),
            creation_placement_scope(
                coordinator.store.replica_id, coordinator.store.endpoint, 90
            ) as created,
        ):
            conv = coordinator.conversations.create_conversation()
            initial = coordinator.store.get("session", conv.id)
            assert initial is not None
            assert coordinator.local(initial)
            task = await create_session_launch_task(conv.id, gate.wait())
        await coordinator.release_created(created)
        assert coordinator.store.get("session", conv.id) == initial
        gate.set()
        await task
        for _ in range(100):
            if coordinator.store.get("session", conv.id) is None:
                break
            await asyncio.sleep(0.01)
        assert coordinator.store.get("session", conv.id) is None
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_unused_initial_placement_is_released_and_fork_is_placed(db_uri):
    from omnigent.db.backend_placement import creation_placement_scope

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    try:
        source = coordinator.conversations.create_conversation()
        assert coordinator.store.get("session", source.id) is None
        with creation_placement_scope(
            coordinator.store.replica_id, coordinator.store.endpoint, 90
        ) as created:
            fork = coordinator.conversations.fork_conversation(source.id)
            assert coordinator.store.get("session", fork.id) is not None
        await coordinator.release_created(created)
        assert coordinator.store.get("session", fork.id) is None
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_rejected_replacement_retires_superseded_host_connection(db_uri):
    from types import SimpleNamespace

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    closed = []

    async def close(**kwargs):
        closed.append(kwargs)

    original = SimpleNamespace(close=close, scope={})
    replacement = SimpleNamespace(close=close, scope={})
    try:
        async with coordinator.connection(original, "host", "example") as serving:
            assert serving
            async with coordinator.connection(replacement, "host", "example") as replacing:
                assert replacing
                # Simulate invalid hello/protocol: exit before registering.
                assert closed == [{"code": 1001, "reason": "backend connection replaced"}]
            assert coordinator.store.get("host", "example") is not None
        assert coordinator.store.get("host", "example") is None
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", [False, True])
async def test_lease_loss_cancels_launch_and_fences_database_writes(db_uri, heartbeat):
    from omnigent.server.replication import create_managed_launch_task
    from omnigent.stores.backend_route_store import BackendRouteStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    other = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
    gate, started = asyncio.Event(), asyncio.Event()
    task = None

    async def launch():
        started.set()
        await gate.wait()
        # The transaction itself must reject stale ownership even before the
        # background renewal loop has noticed it.
        await asyncio.to_thread(
            coordinator.conversations.create_conversation, title="must not exist"
        )

    try:
        with replication_scope(coordinator):
            async with coordinator.request_placement("host") as route:
                task = create_managed_launch_task(launch())
                retain_managed_task(task)
        await started.wait()
        coordinator.store.release(route)
        replacement = other.claim("host", "host")
        if heartbeat:
            await coordinator._renew_once()
        gate.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert other.get("host", "host").token == replacement.token
        assert coordinator.conversations.list_conversations().data == []
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


def test_bundled_session_creation_places_metadata_atomically(db_uri):
    from omnigent.db.backend_placement import creation_placement_scope
    from omnigent.stores.backend_route_store import BackendRouteStore

    store = SqlAlchemyConversationStore(db_uri)
    routes = BackendRouteStore(db_uri, "creator", "http://127.0.0.1:8001")
    with creation_placement_scope("creator", routes.endpoint, 90):
        created = store.create_session_with_agent(
            agent_id="28373c2f7c4d68719e6dbc4b9599b9b0",
            agent_name="bundled-agent",
            agent_bundle_location="28373c2f7c4d68719e6dbc4b9599b9b0/bundle1",
            agent_description=None,
        )
        route = routes.get("session", created.conversation.id)
        assert route is not None and route.replica_id == "creator"
        competitor = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
        assert competitor.claim("session", created.conversation.id) == route


@pytest.mark.asyncio
async def test_first_host_tunnel_adopts_launch_reservation_without_fencing_task(db_uri):
    from types import SimpleNamespace

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    try:
        async with coordinator.request_placement("example") as route:
            async with coordinator.connection(
                SimpleNamespace(scope={}), "host", "example"
            ) as local:
                assert local
                assert coordinator.store.get("host", "example").token == route.token
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_stale_launch_cannot_begin_provider_operation(db_uri):
    from omnigent.db.backend_work_fence import BackendWorkFence, fenced_to_thread, work_fence_scope
    from omnigent.stores.backend_route_store import BackendRouteStore

    a = BackendRouteStore(db_uri, "a", "http://127.0.0.1:8001")
    b = BackendRouteStore(db_uri, "b", "http://127.0.0.1:8002")
    route = a.claim("host", "example")
    fence = BackendWorkFence(db_uri, {(0, "host", "example"): route})
    a.release(route)
    b.claim("host", "example")
    calls = []
    with work_fence_scope(fence), pytest.raises(asyncio.CancelledError):
        await fenced_to_thread(calls.append, "must not run")
    assert calls == []


@pytest.mark.asyncio
async def test_remote_initial_owner_prevents_scheduling_launch(db_uri):
    from omnigent.server.replication import create_session_launch_task
    from omnigent.stores.backend_route_store import BackendRouteStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    other = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
    other.claim("session", "example")
    calls = []

    async def launch():
        calls.append("must not start")

    try:
        with replication_scope(coordinator), pytest.raises(RuntimeError, match="another backend"):
            await create_session_launch_task("example", launch())
        assert calls == []
        assert not coordinator._task_fences
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", [False, True])
@pytest.mark.parametrize("after_scope", [False, True])
@pytest.mark.parametrize("boundary", ["provider", "binding"])
async def test_nested_host_lease_loss_fences_launch_with_live_session_lease(
    db_uri, heartbeat, after_scope, boundary
):
    from types import SimpleNamespace

    from omnigent.server.replication import create_session_launch_task
    from omnigent.stores.backend_route_store import BackendRouteStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    other = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
    started, gate = asyncio.Event(), asyncio.Event()
    calls = []
    conv = coordinator.conversations.create_conversation()

    async def finish():
        started.set()
        await gate.wait()
        from omnigent.db.backend_work_fence import fenced_to_thread

        if boundary == "binding":
            await asyncio.to_thread(coordinator.conversations.replace_runner_id, conv.id, "stale")
        else:
            await fenced_to_thread(calls.append, "must not execute")

    async def close(**kwargs):
        pass

    async def launch():
        if after_scope:
            async with coordinator.connection(
                SimpleNamespace(scope={}, close=close), "host", "nested-host"
            ):
                async with coordinator.reserve("nested-host"):
                    pass
            # Host disconnected and reservation exited; the launch still runs.
            await finish()
        else:
            async with coordinator.reserve("nested-host"):
                await finish()

    task = None
    try:
        with replication_scope(coordinator):
            task = await create_session_launch_task(conv.id, launch())
        await started.wait()
        route = coordinator.store.get("host", "nested-host")
        assert task in coordinator._owned[(0, "host", "nested-host")].tasks.values()
        assert coordinator.store.get("session", conv.id) is not None
        coordinator.store.release(route)
        replacement = other.claim("host", "nested-host")
        if heartbeat:
            await coordinator._renew_once()
        gate.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        await asyncio.gather(*coordinator._cleanup)
        assert calls == []
        assert coordinator.conversations.get_conversation(conv.id).runner_id is None
        assert other.get("host", "nested-host").token == replacement.token
        assert coordinator.store.get("session", conv.id) is None
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.parametrize("operation", ["replace", "rearm"])
def test_lifecycle_write_rechecks_lease_after_outer_provider_boundary(db_uri, operation):
    import time

    from omnigent.db.account_authority import account_checks_scope
    from omnigent.db.backend_work_fence import (
        BackendWorkFence,
        check_backend_work,
        work_fence_scope,
    )
    from omnigent.stores.backend_route_store import BackendRouteStore
    from omnigent.stores.host_store import HostStore

    hosts = HostStore(db_uri)
    a = BackendRouteStore(db_uri, "a", "http://127.0.0.1:8001")
    b = BackendRouteStore(db_uri, "b", "http://127.0.0.1:8002")
    hid = "2c167be5bad7438196cd12a3be5fce08"
    expires = int(time.time()) + 300
    with account_checks_scope(False):
        host = hosts.register_managed_host(
            host_id=hid,
            name="test",
            user_id="alice",
            token="original",
            provider="agent_sandbox",
            sandbox_id="old-sandbox",
            token_expires_at=expires,
        )
        route = a.claim("host", hid)
        fence = BackendWorkFence(db_uri, {(0, "host", hid): route})
        with work_fence_scope(fence):
            check_backend_work()  # The outer provider-boundary check succeeded.
        a.release(route)
        b.claim("host", hid)
        with work_fence_scope(fence), pytest.raises(asyncio.CancelledError):
            if operation == "replace":
                hosts.replace_managed_host_sandbox(
                    host_id=hid,
                    user_id="alice",
                    token="new",
                    provider="agent_sandbox",
                    sandbox_id="new-sandbox",
                    token_expires_at=expires,
                )
            else:
                hosts.rearm_managed_host(
                    hid,
                    sandbox_id="old-sandbox",
                    expected_updated_at=host.updated_at,
                    token="new",
                    token_expires_at=expires,
                )
        assert hosts.get_host(hid).sandbox_id == "old-sandbox"
        assert hosts.resolve_launch_token(hid, "original") is not None
        assert hosts.resolve_launch_token(hid, "new") is None


def test_lifecycle_transaction_holds_placement_until_commit(db_uri):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from omnigent.db.backend_work_fence import (
        BackendWorkFence,
        backend_work_transaction,
        work_fence_scope,
    )
    from omnigent.stores.backend_route_store import BackendRouteStore
    from omnigent.stores.host_store import HostStore

    hosts = HostStore(db_uri)
    a = BackendRouteStore(db_uri, "a", "http://127.0.0.1:8001")
    route = a.claim("host", "example")
    fence = BackendWorkFence(db_uri, {(0, "host", "example"): route})
    started = Event()

    def replace_lease():
        started.set()
        return a.release(route)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with work_fence_scope(fence), hosts._lifecycle_session("test_lock") as session:
            with backend_work_transaction(session):
                future = executor.submit(replace_lease)
                assert started.wait(timeout=5)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
        assert future.result(timeout=5)


def test_replication_rejects_separate_conversation_database(db_uri, tmp_path):
    conversations = SqlAlchemyConversationStore(db_uri, f"sqlite:///{tmp_path / 'separate.db'}")
    with pytest.raises(ValueError, match="one shared metadata and conversation database"):
        ReplicaCoordinator(
            ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
            db_uri,
            conversations,
            HostRegistry(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("before_start", [False, True])
async def test_canceled_launch_settles_tracker_and_allows_retry(db_uri, monkeypatch, before_start):
    from omnigent.server.managed_hosts import ManagedLaunchTracker
    from omnigent.server.replication import create_session_launch_task, managed_launch_cancellation

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    tracker = ManagedLaunchTracker()
    session_id = "retry-session"
    tracker.begin(session_id)
    original = tracker.get(session_id)
    progress = []
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._publish_sandbox_status",
        lambda sid, stage, error=None: progress.append((sid, stage, error)),
    )
    interrupted = managed_launch_cancellation(tracker, session_id)
    started, gate = asyncio.Event(), asyncio.Event()

    async def blocked_launch():
        started.set()
        await gate.wait()

    task = None
    try:
        with replication_scope(coordinator):
            task = await create_session_launch_task(
                session_id, blocked_launch(), on_cancel=interrupted
            )
        if before_start:
            task.cancel()
        else:
            await started.wait()
            coordinator.store.release(coordinator.store.get("session", session_id))
            await coordinator._renew_once()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*coordinator._cleanup)
        assert original.settled.is_set() and "interrupted" in original.error
        assert progress == [(session_id, "failed", original.error)]
        tracker.begin(session_id)
        retry_entry = tracker.get(session_id)
        interrupted()  # A delayed callback must not settle a newer attempt.
        assert not retry_entry.settled.is_set()

        async def retry():
            tracker.finish(session_id)

        with replication_scope(coordinator):
            task = await create_session_launch_task(
                session_id, retry(), on_cancel=managed_launch_cancellation(tracker, session_id)
            )
        await task
        assert retry_entry.settled.is_set() and retry_entry.error is None
        assert tracker.get(session_id) is None
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["request", "connection"])
@pytest.mark.parametrize("intervening_owner", [False, True])
async def test_expired_generation_cannot_regain_launch_tasks_before_heartbeat(
    db_uri, admission, intervening_owner
):
    import time
    from dataclasses import replace
    from types import SimpleNamespace

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlBackendRoute
    from omnigent.db.utils import get_or_create_engine
    from omnigent.server.replication import create_managed_launch_task
    from omnigent.stores.backend_route_store import BackendRouteStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    other = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
    started, gate = asyncio.Event(), asyncio.Event()
    calls = []

    async def launch():
        started.set()
        await gate.wait()
        calls.append("old launch resumed")
        await asyncio.to_thread(coordinator.conversations.create_conversation)

    task = None
    try:
        with replication_scope(coordinator):
            async with coordinator.request_placement("example") as old:
                task = create_managed_launch_task(launch())
                retain_managed_task(task)
        await started.wait()
        fence = coordinator._task_fences[task]
        expired = time.time() - 1
        with Session(get_or_create_engine(db_uri)) as session:
            session.execute(
                update(SqlBackendRoute)
                .where(
                    SqlBackendRoute.workspace_id == 0,
                    SqlBackendRoute.kind == "host",
                    SqlBackendRoute.resource_id == "example",
                )
                .values(expires_at=expired)
            )
            session.commit()
        coordinator._owned[(0, "host", "example")].route = replace(old, expires_at=expired)
        if intervening_owner:
            replacement = other.claim("host", "example")
            assert replacement.replica_id == "other"
            other.release(replacement)
        # No renewal tick has run. Reacquisition itself must retire old work.
        context = (
            coordinator.request_placement("example")
            if admission == "request"
            else coordinator.connection(SimpleNamespace(scope={}), "host", "example")
        )
        async with context:
            new = coordinator.store.get("host", "example")
            assert new is not None and new.token != old.token
            gate.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert fence.lost
            assert calls == []
            assert coordinator.store.get("host", "example").token == new.token
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_live_reconnect_proves_continuity_before_transferring_launch(db_uri):
    from types import SimpleNamespace

    from omnigent.server.replication import create_managed_launch_task

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    started, gate = asyncio.Event(), asyncio.Event()
    closed = []

    async def close(**kwargs):
        closed.append(kwargs)

    async def launch():
        started.set()
        await gate.wait()
        await asyncio.to_thread(coordinator.conversations.create_conversation, title="continuous")

    task = None
    try:
        with replication_scope(coordinator):
            async with coordinator.connection(
                SimpleNamespace(scope={}, close=close), "host", "example"
            ):
                old = coordinator.store.get("host", "example")
                task = create_managed_launch_task(launch())
                retain_managed_task(task)
                await started.wait()
                async with coordinator.connection(
                    SimpleNamespace(scope={}, close=close), "host", "example"
                ):
                    new = coordinator.store.get("host", "example")
                    assert new.token == old.token
                    assert new.connection_token != old.connection_token
                    assert (
                        coordinator._task_fences[task].routes[(0, "host", "example")].token
                        == new.token
                    )
                    gate.set()
                    await task
                    assert closed
        assert coordinator.conversations.list_conversations().data[0].title == "continuous"
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["after-commit", "old-snapshot"])
async def test_launch_validation_across_live_socket_handover(db_uri, monkeypatch, window):
    from contextlib import asynccontextmanager
    from threading import Event
    from types import SimpleNamespace

    from omnigent.db.backend_work_fence import check_backend_work, work_fence_scope
    from omnigent.server.replication import create_managed_launch_task

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    launch_started, launch_gate = asyncio.Event(), asyncio.Event()
    committed, handover_gate, connected, socket_gate = (asyncio.Event() for _ in range(4))
    snapshot_ready, snapshot_gate = Event(), Event()

    async def close(**kwargs):
        pass

    old_ws, new_ws = SimpleNamespace(scope={}, close=close), SimpleNamespace(scope={}, close=close)
    original_reference = coordinator._reference

    @asynccontextmanager
    async def paused_handover(route, ws):
        if ws is new_ws:
            committed.set()
            await handover_gate.wait()
        async with original_reference(route, ws):
            yield

    monkeypatch.setattr(coordinator, "_reference", paused_handover)

    class SnapshotRoutes(dict):
        def items(self):
            snapshot = list(super().items())
            snapshot_ready.set()
            assert snapshot_gate.wait(timeout=5)
            return snapshot

    async def launch():
        launch_started.set()
        await launch_gate.wait()
        await asyncio.to_thread(coordinator.conversations.create_conversation, title="healthy")

    async def reconnect():
        async with coordinator.connection(new_ws, "host", "example"):
            connected.set()
            await socket_gate.wait()

    task = validation = reconnecting = None
    try:
        with replication_scope(coordinator):
            async with coordinator.connection(old_ws, "host", "example"):
                old = coordinator.store.get("host", "example")
                task = create_managed_launch_task(launch())
                retain_managed_task(task)
                await launch_started.wait()
                fence = coordinator._task_fences[task]

                def check():
                    with work_fence_scope(fence):
                        check_backend_work()

                if window == "old-snapshot":
                    fence.routes = SnapshotRoutes(fence.routes)
                    validation = asyncio.create_task(asyncio.to_thread(check))
                    assert await asyncio.to_thread(snapshot_ready.wait, 5)
                reconnecting = asyncio.create_task(reconnect())
                await asyncio.wait_for(committed.wait(), timeout=5)
                # The database committed; the coordinator still holds its old route.
                current = coordinator.store.get("host", "example")
                assert current.token == old.token
                assert current.connection_token != old.connection_token
                assert (
                    coordinator._owned[(0, "host", "example")].route.connection_token
                    == old.connection_token
                )
                if window == "after-commit":
                    validation = asyncio.create_task(asyncio.to_thread(check))
                snapshot_gate.set()
                await validation
                assert not fence.lost
                handover_gate.set()
                await asyncio.wait_for(connected.wait(), timeout=5)
                launch_gate.set()
                await task
                socket_gate.set()
                await reconnecting
        assert coordinator.conversations.list_conversations().data[0].title == "healthy"
    finally:
        snapshot_gate.set()
        launch_gate.set()
        handover_gate.set()
        socket_gate.set()
        pending = [t for t in (task, validation, reconnecting) if t is not None]
        for t in pending:
            if not t.done():
                t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", [False, True])
async def test_ownership_socket_close_does_not_mark_process_shutdown(db_uri, monkeypatch, loss):
    from types import SimpleNamespace

    from omnigent.server import shutdown_state

    monkeypatch.setattr(shutdown_state, "_marked_at", None)
    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    closed = []

    async def close(**kwargs):
        closed.append(kwargs["code"])
        shutdown_state.note_tunnel_close_code(kwargs["code"])

    try:
        async with coordinator.connection(
            SimpleNamespace(scope={}, close=close), "host", "example"
        ):
            if loss:
                route = coordinator.store.get("host", "example")
                coordinator.store.release(route)
                await coordinator._renew_once()
            else:
                async with coordinator.connection(
                    SimpleNamespace(scope={}, close=close), "host", "example"
                ):
                    pass
            assert closed == [1001]
            assert not shutdown_state.server_shutting_down()
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_retained_launch_reconnect_rotates_socket_before_delayed_readiness(db_uri):
    import threading
    from types import SimpleNamespace

    from omnigent.server.replication import create_managed_launch_task
    from omnigent.stores.host_store import HostStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    hosts = HostStore(db_uri)
    started, release = threading.Event(), threading.Event()
    launch_gate = asyncio.Event()

    async def close(**kwargs):
        pass

    def delayed_readiness():
        started.set()
        assert release.wait(timeout=5)
        hosts.update_harness_readiness("6d86ee544f1d5b7068ac56f5927a5b5c", {"pi": False})

    launch = write = None
    try:
        with replication_scope(coordinator):
            async with coordinator.connection(
                SimpleNamespace(scope={}, close=close), "host", "6d86ee544f1d5b7068ac56f5927a5b5c"
            ):
                await asyncio.to_thread(
                    hosts.upsert_on_connect,
                    "6d86ee544f1d5b7068ac56f5927a5b5c",
                    "original",
                    "local",
                )
                old = coordinator.store.get("host", "6d86ee544f1d5b7068ac56f5927a5b5c")
                launch = create_managed_launch_task(launch_gate.wait())
                retain_managed_task(launch)
                write = asyncio.create_task(asyncio.to_thread(delayed_readiness))
                assert await asyncio.to_thread(started.wait, 2)
            # The socket is gone, but the launch still retains resource ownership.
            assert coordinator.store.get("host", "6d86ee544f1d5b7068ac56f5927a5b5c") is not None
            async with coordinator.connection(
                SimpleNamespace(scope={}, close=close), "host", "6d86ee544f1d5b7068ac56f5927a5b5c"
            ):
                new = coordinator.store.get("host", "6d86ee544f1d5b7068ac56f5927a5b5c")
                assert new.token == old.token
                assert new.connection_token != old.connection_token
                await asyncio.to_thread(
                    hosts.upsert_on_connect,
                    "6d86ee544f1d5b7068ac56f5927a5b5c",
                    "replacement",
                    "local",
                    configured_harnesses={"pi": True},
                )
                release.set()
                await write
                assert hosts.get_host("6d86ee544f1d5b7068ac56f5927a5b5c").configured_harnesses == {
                    "pi": True
                }
                launch_gate.set()
                await launch
    finally:
        release.set()
        launch_gate.set()
        await asyncio.gather(
            *(task for task in (launch, write) if task is not None), return_exceptions=True
        )
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["create_dir", "install", "store_secret", "file_write", "stop"]
)
async def test_host_mutations_cannot_enqueue_after_ownership_loss(db_uri, command):
    from types import SimpleNamespace

    from omnigent.db.backend_work_fence import work_fence_scope
    from omnigent.host.frames import HostHelloFrame, HostStoreSecretFrame
    from omnigent.server.routes._host_filesystem import write_workspace_from_host
    from omnigent.server.routes._sessions.helpers import _stop_session_host_runner
    from omnigent.server.routes.hosts import (
        _proxy_create_dir,
        _proxy_install_harness,
        _proxy_store_secret,
    )
    from omnigent.stores.backend_route_store import BackendRouteStore

    hosts = HostRegistry()
    conn = hosts.register(
        "example",
        SimpleNamespace(),
        HostHelloFrame(version="test", name="host", frame_protocol_version=1),
        owner=None,
    )
    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        hosts,
    )
    other = BackendRouteStore(db_uri, "replacement", "http://127.0.0.1:8002")
    try:
        async with coordinator.request_placement("example") as route:
            with work_fence_scope(None):
                coordinator.store.release(route)
                other.claim("host", "example")
            if command == "create_dir":
                call = _proxy_create_dir(host_registry=hosts, host_conn=conn, path="/tmp/owned")
            elif command == "install":
                call = _proxy_install_harness(host_registry=hosts, host_conn=conn, harness="pi")
            elif command == "store_secret":
                call = _proxy_store_secret(
                    host_registry=hosts,
                    host_conn=conn,
                    frame=HostStoreSecretFrame(
                        request_id="unused", harness="pi", kind="adopt", env_var="TEST_NOT_USED"
                    ),
                )
            elif command == "file_write":
                call = write_workspace_from_host(
                    host_registry=hosts,
                    host_conn=conn,
                    op="github_set_preference",
                    workspace="/tmp/owned",
                    session_id="unused",
                    params={},
                )
            else:
                call = _stop_session_host_runner("unused", "example", "runner", hosts)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, timeout=0.5)
            assert conn.outbound_queue.empty()
            assert not any(
                (
                    conn.pending_create_dirs,
                    conn.pending_installs,
                    conn.pending_secret_writes,
                    conn.pending_fs_requests,
                    conn.pending_stops,
                )
            )
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_initial_placement_cleanup_ignores_lost_request_fence(db_uri, replacement):
    from omnigent.db.backend_placement import creation_placement_scope
    from omnigent.db.backend_work_fence import check_backend_work, work_fence_scope
    from omnigent.stores.backend_route_store import BackendRouteStore

    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    other = BackendRouteStore(db_uri, "other", "http://127.0.0.1:8002")
    try:
        async with coordinator.request_placement("host") as host:
            with creation_placement_scope(
                coordinator.store.replica_id, coordinator.store.endpoint, 90
            ) as created:
                conv = coordinator.conversations.create_conversation()
            initial = coordinator.store.get("session", conv.id)
            assert initial is not None
            with work_fence_scope(None):
                coordinator.store.release(host)
                other.claim("host", "host")
                if replacement:
                    coordinator.store.release(initial)
                    newer = other.claim("session", conv.id)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.to_thread(check_backend_work)
            await coordinator.release_created(created)
            assert coordinator.conversations.get_conversation(conv.id) is not None
            actual = other.get("session", conv.id)
            if replacement:
                assert actual is not None and actual.token == newer.token
            else:
                assert actual is None
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_locked_postgres_lease_does_not_delay_other_renewals(db_uri):
    import threading
    import time
    from types import SimpleNamespace

    from sqlalchemy import select, update
    from sqlalchemy.engine import make_url

    from omnigent.db.db_models import SqlBackendRoute
    from omnigent.db.utils import get_or_create_engine

    if make_url(db_uri).get_backend_name() != "postgresql":
        pytest.skip("Requires independent PostgreSQL row locks")
    coordinator = ReplicaCoordinator(
        ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )
    engine = get_or_create_engine(db_uri)
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with engine.begin() as connection:
            connection.execute(
                select(SqlBackendRoute)
                .where(SqlBackendRoute.resource_id == "blocked")
                .with_for_update()
            ).all()
            locked.set()
            assert release.wait(10)

    async def close(**kwargs):
        raise AssertionError("Healthy connections must not be retired")

    worker = None
    try:
        async with (
            coordinator.connection(SimpleNamespace(scope={}, close=close), "runner", "blocked"),
            coordinator.connection(SimpleNamespace(scope={}, close=close), "runner", "healthy"),
        ):
            with engine.begin() as connection:
                connection.execute(
                    update(SqlBackendRoute)
                    .where(SqlBackendRoute.resource_id == "healthy")
                    .values(expires_at=time.time() + 0.8)
                )
            worker = asyncio.create_task(asyncio.to_thread(hold_lock))
            assert await asyncio.to_thread(locked.wait, 5)
            await asyncio.wait_for(coordinator._renew_once(), 5)
            healthy = coordinator.store.get("runner", "healthy")
            assert healthy is not None and healthy.expires_at > time.time() + 30
            assert not release.is_set()
            release.set()
            await worker
            await coordinator._renew_once()
            assert coordinator.store.get("runner", "blocked") is not None
    finally:
        release.set()
        if worker is not None:
            await worker
        await coordinator.aclose()
