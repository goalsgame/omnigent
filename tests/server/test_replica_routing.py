"""Request forwarding preserves bodies, authority and cold-start single flight."""

import asyncio
import hashlib
from ipaddress import ip_network

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from omnigent.db.account_authority import account_checks_scope
from omnigent.server.host_registry import HostRegistry
from omnigent.server.replica_routing import ReplicaRoutingMiddleware
from omnigent.server.replica_transport import ReplicationConfig
from omnigent.server.replication import ReplicaCoordinator, retain_managed_task
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore


def coordinator(db_uri, port):
    return ReplicaCoordinator(
        ReplicationConfig(f"http://127.0.0.1:{port}", (ip_network("127.0.0.0/8"),), b"s" * 32),
        db_uri,
        SqlAlchemyConversationStore(db_uri),
        HostRegistry(),
    )


@pytest.fixture(autouse=True)
def supported_database(db_uri):
    from sqlalchemy.engine import make_url

    if make_url(db_uri).get_backend_name() not in {"sqlite", "postgresql"}:
        pytest.skip("Replica placement supports SQLite and PostgreSQL")


@pytest.mark.asyncio
async def test_two_cold_requests_choose_one_backend_and_retain_background_launch(db_uri):
    a, b = coordinator(db_uri, 8001), coordinator(db_uri, 8002)
    hosts = HostStore(db_uri)
    hid = "2c167be5bad7438196cd12a3be5fce08"
    with account_checks_scope(False):
        hosts.upsert_on_connect(hid, "test-host", "alice")
    hosts.set_offline(hid)
    gate = asyncio.Event()
    started = []
    pending = {}

    def backend(name):
        async def endpoint(scope, receive, send):
            if name not in pending:
                pending[name] = asyncio.create_task(gate.wait())
                retain_managed_task(pending[name])
                started.append(name)
            await JSONResponse({"backend": name})(scope, receive, send)

        return endpoint

    apps = {
        8001: ReplicaRoutingMiddleware(backend("a"), a, None, host_store=hosts),
        8002: ReplicaRoutingMiddleware(backend("b"), b, None, host_store=hosts),
    }

    class Dispatcher(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await httpx.ASGITransport(app=apps[request.url.port]).handle_async_request(
                request
            )

    for replica in (a, b):
        await replica.transport._client._transport.aclose()
        replica.transport._client._transport = Dispatcher()
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[8001]), base_url="https://public.example"
            ) as ca,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[8002]), base_url="https://public.example"
            ) as cb,
        ):
            first, second = await asyncio.gather(
                ca.post(f"/v1/hosts/{hid}/runners"), cb.post(f"/v1/hosts/{hid}/runners")
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["backend"] == second.json()["backend"]
            assert len(started) == 1
            assert a.store.get("host", hid) is not None
        gate.set()
        await asyncio.gather(*pending.values())
        for _ in range(100):
            if a.store.get("host", hid) is None:
                break
            await asyncio.sleep(0.01)
        assert a.store.get("host", hid) is None
        await asyncio.gather(*a._cleanup, *b._cleanup)
        assert not a._locks and not b._locks
    finally:
        gate.set()
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "invalid-json", "multipart", "multipart-partial-error"])
async def test_body_replay_is_byte_exact_even_if_form_parsing_stops_early(db_uri, mode):
    replica = coordinator(db_uri, 8001)

    async def endpoint(scope, receive, send):
        body = await Request(scope, receive).body()
        await JSONResponse({"sha256": hashlib.sha256(body).hexdigest()})(scope, receive, send)

    app = ReplicaRoutingMiddleware(endpoint, replica, None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.example"
        ) as client:
            if mode == "multipart":
                request = client.build_request(
                    "POST",
                    "/v1/sessions",
                    data={"metadata": "{}"},
                    files={"bundle": ("agent.tar.gz", b"\x00\xffpayload")},
                )
                expected = await request.aread()
            else:
                expected = b"{}" if mode == "json" else b"{invalid json"
                headers = {"Content-Type": "application/json"}
                if mode == "multipart-partial-error":
                    first = (
                        b'--boundary\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
                        + b"x" * (1024 * 1024 + 1)
                    )
                    last = b"\r\n--boundary--\r\nTRAILING_BYTES"
                    expected = first + last
                    headers = {"Content-Type": "multipart/form-data; boundary=boundary"}

                    async def pieces():
                        yield first
                        yield last

                    request = client.build_request(
                        "POST", "/v1/sessions", content=pieces(), headers=headers
                    )
                else:
                    request = client.build_request(
                        "POST", "/v1/sessions", content=expected, headers=headers
                    )
            response = await client.send(request)
            assert response.status_code == 200, response.text
            assert response.json()["sha256"] == hashlib.sha256(expected).hexdigest()
    finally:
        await replica.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["skills-host", "skills-session", "import", "import-stream", "clear-runner"]
)
async def test_host_dependent_endpoints_forward_to_owner(db_uri, endpoint):
    a, b = coordinator(db_uri, 8001), coordinator(db_uri, 8002)
    hid = "2c167be5bad7438196cd12a3be5fce08"
    conv = a.conversations.create_conversation(host_id=hid, workspace="/workspace")
    observed = []

    def backend(name):
        async def serve(scope, receive, send):
            observed.append((name, await Request(scope, receive).body()))
            await JSONResponse({"backend": name})(scope, receive, send)

        return serve

    apps = {
        8001: ReplicaRoutingMiddleware(backend("a"), a, None),
        8002: ReplicaRoutingMiddleware(backend("b"), b, None),
    }

    class Dispatcher(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await httpx.ASGITransport(app=apps[request.url.port]).handle_async_request(
                request
            )

    await b.transport._client._transport.aclose()
    b.transport._client._transport = Dispatcher()
    try:
        async with (
            a.request_placement(hid),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[8002]), base_url="https://public.example"
            ) as client,
        ):
            if endpoint.startswith("skills"):
                key, value = (
                    ("host_id", hid) if endpoint == "skills-host" else ("session_id", conv.id)
                )
                response = await client.get("/v1/skills", params={key: value})
            elif endpoint == "clear-runner":
                response = await client.patch(f"/v1/sessions/{conv.id}", json={"runner_id": ""})
            else:
                path = "/v1/imports/local" + ("/stream" if endpoint == "import-stream" else "")
                response = await client.post(path, json={"host_id": hid, "source": "pi"})
            assert response.status_code == 200
            assert response.json() == {"backend": "a"}
            assert len(observed) == 1
            if endpoint == "clear-runner":
                import json

                assert json.loads(observed[0][1]) == {"runner_id": ""}
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", [8001, 8002])
@pytest.mark.parametrize("runner_id", ["new-runner", " new-runner "])
async def test_runner_rebind_preserves_explicit_destination_across_peer_hop(
    db_uri, ingress, runner_id
):
    a, b = coordinator(db_uri, 8001), coordinator(db_uri, 8002)
    hid = "2c167be5bad7438196cd12a3be5fce08"
    conv = a.conversations.create_conversation(host_id=hid, workspace="/source")
    calls = []
    restarted = None

    def endpoint(name, replica):
        async def serve(scope, receive, send):
            request = Request(scope, receive)
            if scope["path"] == "/v1/runners":
                route = await replica.runner_route("new-runner")
                data = (
                    [{"runner_id": "new-runner", "online": True}]
                    if route is not None and replica.local(route)
                    else []
                )
                data.extend(await replica.remote_runners(request))
                response = {"data": data}
            else:
                if request.method == "PATCH":
                    body = await request.json()
                    replica.conversations.replace_runner_id(conv.id, body["runner_id"].strip())
                else:
                    body = await request.body()
                calls.append((name, body))
                response = {"backend": name}
            await JSONResponse(response)(scope, receive, send)

        return serve

    apps = {
        8001: ReplicaRoutingMiddleware(endpoint("a", a), a, None),
        8002: ReplicaRoutingMiddleware(endpoint("b", b), b, None),
    }

    class Dispatcher(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await httpx.ASGITransport(app=apps[request.url.port]).handle_async_request(
                request
            )

    for replica in (a, b):
        await replica.transport._client._transport.aclose()
        replica.transport._client._transport = Dispatcher()
    try:
        async with (
            a.request_placement(hid),
            b.request_resource("runner", "new-runner"),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[ingress]), base_url="https://public.example"
            ) as client,
        ):
            from omnigent.db.backend_work_fence import work_fence_scope

            # The two placement contexts above model other requests/backends.
            with work_fence_scope(None):
                response = await client.patch(
                    f"/v1/sessions/{conv.id}", json={"runner_id": runner_id}
                )
                assert response.status_code == 200
                assert response.json() == {"backend": "b"}
                assert calls == [("b", {"runner_id": runner_id})]
                bound = a.conversations.get_conversation(conv.id)
                assert bound.runner_id == "new-runner" and bound.host_id == hid
                assert (await a.runner_route("new-runner")).replica_id == b.store.replica_id
                for path, method in [
                    (f"/v1/sessions/{conv.id}", "GET"),
                    (f"/v1/sessions/{conv.id}/messages", "POST"),
                    ("/v1/runners/new-runner", "GET"),
                ]:
                    followup = await client.request(method, path)
                    assert followup.status_code == 200
                    assert followup.json() == {"backend": "b"}
                inventory = await client.get("/v1/runners")
                assert inventory.status_code == 200
                assert inventory.json() == {"data": [{"runner_id": "new-runner", "online": True}]}
                assert all(name == "b" for name, _ in calls)
                skills = await client.get("/v1/skills", params={"session_id": conv.id})
                assert skills.status_code == 200
                assert skills.json() == {"backend": "a"}
                # An offline runner still permits host-backed workspace reads.
                b.store.release(b.store.get("runner", "new-runner"))
                fallback = await client.get(f"/v1/sessions/{conv.id}/resources/github")
                assert fallback.status_code == 200 and fallback.json() == {"backend": "a"}
        # Both old sockets/leases are gone, and the original host is offline.
        assert a.store.get("host", hid) is None
        assert b.store.get("runner", "new-runner") is None
        assert b.store.is_standalone_runner("new-runner")
        await b.aclose()
        restarted = coordinator(db_uri, 8002)
        apps[8002] = ReplicaRoutingMiddleware(endpoint("b", restarted), restarted, None)
        from types import SimpleNamespace

        async with restarted.connection(
            SimpleNamespace(scope={}), "runner", "new-runner"
        ) as local:
            assert local
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[8001]), base_url="https://public.example"
            ) as client:
                status = await client.get("/v1/runners/new-runner/status")
                assert status.status_code == 200 and status.json() == {"backend": "b"}
                session = await client.get(f"/v1/sessions/{conv.id}")
                assert session.status_code == 200 and session.json() == {"backend": "b"}
                inventory = await client.get("/v1/runners")
                assert inventory.json() == {"data": [{"runner_id": "new-runner", "online": True}]}
    finally:
        await a.aclose()
        await b.aclose()
        if restarted is not None:
            await restarted.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type", ["application/vnd.api+json", "Application/JSON; charset=utf-8"]
)
async def test_create_routes_all_supported_json_media_types(db_uri, content_type):
    a, b = coordinator(db_uri, 8001), coordinator(db_uri, 8002)
    hid = "2c167be5bad7438196cd12a3be5fce08"
    observed = []

    async def owner(scope, receive, send):
        observed.append(await Request(scope, receive).json())
        await JSONResponse({"owner": "b"})(scope, receive, send)

    async def wrong(scope, receive, send):
        pytest.fail("Host-backed create reached the wrong backend")

    await a.transport._client.aclose()
    a.transport._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ReplicaRoutingMiddleware(owner, b, None))
    )
    try:
        async with b.request_placement(hid):
            from omnigent.db.backend_work_fence import work_fence_scope

            with work_fence_scope(None):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=ReplicaRoutingMiddleware(wrong, a, None)),
                    base_url="https://public.example",
                ) as client:
                    response = await client.post(
                        "/v1/sessions",
                        content='{"host_id":"' + hid + '"}',
                        headers={"Content-Type": content_type},
                    )
                    assert response.status_code == 200
                    assert observed == [{"host_id": hid}]
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["provider", "transaction"])
@pytest.mark.parametrize("heartbeat", [False, True])
@pytest.mark.parametrize(
    "mode", ["delete", "patch", "host-create", "runner-create", "import", "import-stream"]
)
async def test_local_http_mutation_is_fenced_after_ownership_loss(
    db_uri, boundary, heartbeat, mode
):
    from contextlib import suppress

    from omnigent.db.backend_work_fence import fenced_to_thread

    a, b = coordinator(db_uri, 8001), coordinator(db_uri, 8002)
    conv = a.conversations.create_conversation()
    kind, resource = (
        ("runner", "selected-runner")
        if mode in {"patch", "runner-create"}
        else ("host", "2c167be5bad7438196cd12a3be5fce08")
        if mode in {"host-create", "import", "import-stream"}
        else ("session", conv.id)
    )
    if mode != "delete":
        a.store.claim(kind, resource)
    started, gate = asyncio.Event(), asyncio.Event()
    writes = []

    async def mutation(scope, receive, send):
        started.set()
        # Even a handler that catches cancellation cannot cross a write boundary.
        with suppress(asyncio.CancelledError):
            await gate.wait()
        if boundary == "provider":
            await fenced_to_thread(writes.append, "provider-called")
        else:
            await asyncio.to_thread(a.conversations.replace_runner_id, conv.id, "stale-runner")
        await JSONResponse({"unexpected": "success"})(scope, receive, send)

    task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ReplicaRoutingMiddleware(mutation, a, None)),
            base_url="https://public.example",
        ) as client:
            if mode == "patch":
                operation = client.patch(f"/v1/sessions/{conv.id}", json={"runner_id": resource})
            elif mode in {"host-create", "runner-create"}:
                operation = client.post("/v1/sessions", json={f"{kind}_id": resource})
            elif mode in {"import", "import-stream"}:
                path = "/v1/imports/local" + ("/stream" if mode == "import-stream" else "")
                operation = client.post(path, json={"host_id": resource})
            else:
                operation = client.delete(f"/v1/sessions/{conv.id}")
            task = asyncio.create_task(operation)
            await started.wait()
            route = a.store.get(kind, resource)
            assert route is not None
            a.store.release(route)
            assert b.store.claim(kind, resource).replica_id == b.store.replica_id
            if heartbeat:
                await a._renew_once()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert writes == []
            assert a.conversations.get_conversation(conv.id).runner_id is None
    finally:
        gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await a.aclose()
        await b.aclose()
