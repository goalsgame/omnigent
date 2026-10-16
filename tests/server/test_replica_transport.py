"""Peer routing cannot replace client auth or alter origin-sensitive checks."""

from dataclasses import replace
from ipaddress import ip_network

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from omnigent.server.replica_transport import PEER_HEADER, PeerTransport, ReplicationConfig
from omnigent.stores.backend_route_store import BackendRoute


@pytest.fixture
def config():
    return ReplicationConfig("http://127.0.0.1:8001", (ip_network("127.0.0.0/8"),), b"x" * 32)


def scope():
    return {
        "type": "http",
        "method": "POST",
        "path": "/v1/sessions/example/items",
        "raw_path": b"/v1/sessions/example/items",
        "query_string": b"q=a%2Fb",
        "headers": [(b"host", b"public.example"), (b"authorization", b"Bearer user-token")],
        "scheme": "https",
        "client": ("203.0.113.10", 1234),
        "root_path": "",
    }


@pytest.mark.asyncio
async def test_peer_context_preserves_origin_and_rejects_tampering(config):
    peer = PeerTransport(config)
    original = scope()
    route = BackendRoute(0, "host", "example", "b", "http://127.0.0.1:8002", "nonce", 999999)
    try:
        signed = peer._context(original, route).encode()
        received = {
            **original,
            "scheme": "http",
            "client": ("127.0.0.1", 9000),
            "headers": [(b"host", b"127.0.0.1:8002"), (PEER_HEADER, signed)],
        }
        context = peer.verify_context(received)
        restored = peer.restore_origin(received, context)
        assert restored["client"] == original["client"]
        assert restored["scheme"] == "https"
        assert dict(restored["headers"])[b"host"] == b"public.example"
        assert PEER_HEADER not in dict(restored["headers"])
        for changed in [
            {"path": "/v1/admin"},
            {"query_string": b"q=other"},
            {"method": "DELETE"},
            {"raw_path": b"/v1/sessions/%65xample/items"},
            {"headers": [(PEER_HEADER, signed + b"0")]},
            {"headers": [(PEER_HEADER, signed), (PEER_HEADER, signed)]},
        ]:
            with pytest.raises(ValueError, match="invalid backend peer"):
                peer.verify_context({**received, **changed})
    finally:
        await peer.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254",
        "http://example.com",
        "http://127.0.0.1/admin",
        "http://user@127.0.0.1",
        "http://127.0.0.1?x=y",
        "http://127.0.0.1#fragment",
        "ftp://127.0.0.1",
        "http://127.0.0.1:0",
        "http://[fe80::1%eth0]",
    ],
)
def test_peer_urls_cannot_escape_operator_networks(config, url):
    with pytest.raises(ValueError):
        config.validate_endpoint(url)


def test_configuration_requires_explicit_networks_and_secret(monkeypatch):
    monkeypatch.setenv("OMNIGENT_REPLICA_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("OMNIGENT_REPLICA_KEY", "a" * 32)
    config = ReplicationConfig.parse({"allowed_networks": ["127.0.0.0/8"]})
    assert config.endpoint == "http://127.0.0.1:8001"
    assert "a" * 32 not in repr(config)
    for raw in [
        {},
        {"allowed_networks": ["0.0.0.0/0"]},
        {"unknown": True},
        {"allowed_networks": ["127.0.0.0/8"], "lease_seconds": True},
    ]:
        with pytest.raises(ValueError):
            ReplicationConfig.parse(raw)
    monkeypatch.setenv("OMNIGENT_REPLICA_KEY", "short")
    with pytest.raises(ValueError, match="peer key"):
        ReplicationConfig.parse({"allowed_networks": ["127.0.0.0/8"]})


@pytest.mark.asyncio
async def test_forwarding_preserves_auth_body_and_origin_without_cookie_leak(config):
    transport = PeerTransport(config)
    owner = PeerTransport(replace(config, endpoint="http://127.0.0.1:8002"))
    route = BackendRoute(0, "host", "example", "b", owner.config.endpoint, "nonce", 999999)
    observed = []

    async def backend(scope, receive, send):
        context = owner.verify_context(scope)
        request = Request(owner.restore_origin(scope, context), receive)
        observed.append(dict(request.headers))
        if request.headers.get("authorization") != "Bearer user-token":
            await Response(status_code=401)(scope, receive, send)
            return
        assert str(request.url).startswith("https://public.example/")
        assert request.client.host == "203.0.113.10"
        response = JSONResponse({"body": (await request.body()).decode()})
        response.set_cookie("backend_cookie", "must-not-leak")
        await response(scope, receive, send)

    await transport._client.aclose()
    from http.cookiejar import CookieJar, DefaultCookiePolicy

    transport._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend),
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
    )

    async def ingress(asgi_scope, receive, send):
        original = {**asgi_scope, "client": ("203.0.113.10", 1234)}
        await transport.forward_http(original, receive, send, route)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ingress), base_url="https://public.example"
        ) as browser:
            for body in ["first", "second"]:
                browser.cookies.clear()
                response = await browser.post(
                    "/v1/sessions/example/items",
                    content=body,
                    headers={"Authorization": "Bearer user-token"},
                )
                assert response.status_code == 200
                assert response.json() == {"body": body}
            browser.cookies.clear()
            assert (await browser.post("/v1/sessions/example/items")).status_code == 401
        assert all("cookie" not in headers for headers in observed)
    finally:
        await transport.aclose()
        await owner.aclose()
