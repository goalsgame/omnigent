"""Placement is retained until background launch tasks settle."""

import asyncio
from ipaddress import ip_network

import pytest

from omnigent.server.host_registry import HostRegistry
from omnigent.server.replica_transport import ReplicationConfig
from omnigent.server.replication import ReplicaCoordinator, replication_scope, retain_managed_task
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


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
    from omnigent.server.replication import retain_created_session_task

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
            task = asyncio.create_task(gate.wait())
            await retain_created_session_task(conv.id, task)
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
