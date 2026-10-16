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
                assert closed == [{"code": 1012, "reason": "backend connection replaced"}]
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
async def test_nested_host_lease_loss_fences_launch_with_live_session_lease(db_uri, heartbeat):
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

    async def launch():
        async with coordinator.reserve("nested-host"):
            started.set()
            await gate.wait()
            from omnigent.db.backend_work_fence import fenced_to_thread

            await fenced_to_thread(calls.append, "must not execute")

    task = None
    try:
        with replication_scope(coordinator):
            task = await create_session_launch_task("new-session", launch())
        await started.wait()
        route = coordinator.store.get("host", "nested-host")
        assert task in coordinator._owned[(0, "host", "nested-host")].tasks.values()
        assert coordinator.store.get("session", "new-session") is not None
        coordinator.store.release(route)
        replacement = other.claim("host", "nested-host")
        if heartbeat:
            await coordinator._renew_once()
        gate.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        await asyncio.gather(*coordinator._cleanup)
        assert calls == []
        assert other.get("host", "nested-host").token == replacement.token
        assert coordinator.store.get("session", "new-session") is None
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
                    assert new.token != old.token
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
