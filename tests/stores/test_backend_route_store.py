"""Cross-replica ownership, expiry and delayed cleanup fencing."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.backend_route_store import BackendRouteStore


@pytest.fixture
def replicas(db_uri):
    uri = db_uri
    clock = [1000.0]
    return (
        BackendRouteStore(uri, "a", "http://127.0.0.1:8001", now=lambda: clock[0]),
        BackendRouteStore(uri, "b", "http://127.0.0.1:8002", now=lambda: clock[0]),
        clock,
    )


def test_live_owner_keeps_traffic(replicas):
    a, b, _ = replicas
    claim = a.claim("host", "example")
    assert b.claim("host", "example") == claim
    assert b.get("host", "example") == claim
    assert not b.renew(claim)
    assert not b.release(claim)


def test_expired_owner_cannot_renew_or_remove_replacement(replicas):
    a, b, clock = replicas
    old = a.claim("host", "example")
    clock[0] += a.ttl
    assert a.get("host", "example") is None
    assert not a.renew(old)
    new = b.claim("host", "example")
    assert new.replica_id == "b"
    assert not a.release(old)
    assert not a.renew(old)
    assert a.get("host", "example") == new


def test_reconnect_fences_delayed_same_replica_disconnect(replicas):
    a, _, _ = replicas
    first = a.claim("runner", "example")
    second = a.claim("runner", "example")
    assert second.token != first.token
    assert not a.renew(first)
    assert not a.release(first)
    assert a.renew(second)
    assert a.release(second)
    assert a.get("runner", "example") is None


def test_heartbeats_extend_lease(replicas):
    a, b, clock = replicas
    claim = a.claim("host", "example")
    clock[0] += 60
    assert a.renew(claim)
    clock[0] += 60
    assert b.claim("host", "example").replica_id == "a"


def test_reported_host_capabilities_follow_current_lease(replicas):
    a, b, clock = replicas
    claim = a.claim("host", "example")
    assert a.renew(claim, {"version": "test", "interactive_shells": ["powershell"]})
    assert b.host_metadata(["example"])["example"]["interactive_shells"] == ["powershell"]
    clock[0] += a.ttl
    assert b.host_metadata(["example"]) == {}
    b.claim("host", "example")
    assert not a.renew(claim, {"interactive_shells": ["bash"]})
    assert b.host_metadata(["example"]) == {}


def test_workspace_and_resource_kind_are_isolated(replicas):
    a, b, _ = replicas
    with workspace_scope(11):
        host = a.claim("host", "example")
        runner = b.claim("runner", "example")
        assert host.replica_id == "a"
        assert runner.replica_id == "b"
    with workspace_scope(12):
        assert a.get("host", "example") is None
        assert not a.release(host)
        assert not a.renew(host)
        assert b.claim("host", "example").replica_id == "b"
    with workspace_scope(11):
        assert a.get("host", "example") == host


def test_concurrent_first_claim_has_one_owner(replicas):
    a, b, _ = replicas
    start = Barrier(2)

    def claim(store):
        start.wait(timeout=5)
        return store.claim("host", "example")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [a, b]))
    assert results[0] == results[1]
    assert a.get("host", "example") == results[0]


def test_old_host_heartbeat_and_disconnect_cannot_overwrite_new_connection(db_uri):
    from omnigent.db.account_authority import account_checks_scope
    from omnigent.db.backend_fence import backend_fence_scope
    from omnigent.stores.host_store import HostStore

    store = BackendRouteStore(db_uri, "backend", "http://127.0.0.1:8001")
    hosts = HostStore(db_uri)
    host_id = "0e508b33891c40098b0ff214c993fd98"
    old = store.claim("host", host_id)
    with account_checks_scope(False), backend_fence_scope(host_id, old.token):
        hosts.upsert_on_connect(host_id, "test-host", "alice")
    new = store.claim("host", host_id)
    with backend_fence_scope(host_id, old.token):
        hosts.set_offline(host_id)
        hosts.update_harness_readiness(host_id, {"pi": False})
    assert hosts.get_host(host_id).status == "online"
    assert hosts.get_host(host_id).configured_harnesses is None
    with backend_fence_scope(host_id, new.token):
        hosts.set_offline(host_id)
    with backend_fence_scope(host_id, old.token):
        hosts.heartbeat(host_id)
    assert hosts.get_host(host_id).status == "offline"
