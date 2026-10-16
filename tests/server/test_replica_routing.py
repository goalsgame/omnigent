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
