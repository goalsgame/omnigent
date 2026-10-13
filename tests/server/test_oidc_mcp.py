"""Owner isolation, exchange failures and refresh behavior for outbound MCP."""

import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from omnigent.server.oidc_mcp import (
    DelegationUnavailable,
    MCPDelegationBroker,
    MCPDelegationConfig,
)
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.server.test_credential_store import _FakeCipher


@pytest.fixture
def broker(db_uri, monkeypatch):
    config = MCPDelegationConfig(
        "https://mcp.example.com/mcp", "mcp-api", "openid mcp-api", "machine-exchange", "secret"
    )
    oidc = SimpleNamespace(
        provider_type="oidc",
        client_id="app",
        client_secret="secret",
        issuer="https://id.example.com",
        jwks_uri="https://id.example.com/keys",
        token_endpoint="https://id.example.com/token",
    )
    machine = SimpleNamespace(
        principal_allowed=lambda owner: owner == "oidc-machine:bot",
        config=SimpleNamespace(clients={"bot": "bot-sub"}),
        authenticate=lambda token: None,
    )
    auth = SimpleNamespace(machine_verifier=machine, human_verifier=None)
    requests = []

    def respond(request):
        form = parse_qs(request.content.decode())
        requests.append(form)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "rotated",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    claims = {"sub": "person-sub", "azp": "app", "exp": time.time() + 300}
    monkeypatch.setattr("omnigent.server.oidc_mcp.verify_access_token", lambda *args: claims)
    result = MCPDelegationBroker(
        config, oidc, CredentialStore(db_uri, _FakeCipher()), auth, client
    )
    return result, claims, requests


def test_owner_isolation_and_refresh(broker):
    service, _claims, requests = broker
    service.capture("alice@example.com", "person-sub", "original")
    with pytest.raises(DelegationUnavailable, match="No MCP identity"):
        service.credential("bob@example.com")
    assert service.credential("alice@example.com") == "access"
    assert len(requests) == 1
    connection = service.store.get("alice@example.com", "oidc_mcp", with_secret=True)
    service.store.update_secret(
        "alice@example.com", "oidc_mcp", secret={**connection.secret, "expires_at": 0}
    )
    assert service.credential("alice@example.com") == "access"
    assert requests[-1]["grant_type"] == ["refresh_token"]
    assert requests[-1]["refresh_token"] == ["rotated"]


@pytest.mark.parametrize(
    "field,value", [("sub", "other-person"), ("azp", "other-client"), ("exp", 0)]
)
def test_exchange_rejects_wrong_identity(broker, field, value):
    service, claims, _ = broker
    claims[field] = value
    with pytest.raises(DelegationUnavailable):
        service.capture("alice@example.com", "person-sub", "original")
    assert service.store.get("alice@example.com", "oidc_mcp") is None


def test_machine_binding_checked_on_each_call(broker):
    service, claims, requests = broker
    claims.update(sub="bot-sub", azp="machine-exchange")
    service.capture("oidc-machine:bot", "bot-sub", "original")
    assert requests[-1]["client_id"] == ["machine-exchange"]
    assert service.credential("oidc-machine:bot") == "access"
    service.auth.machine_verifier.config.clients["bot"] = "replacement-sub"
    with pytest.raises(DelegationUnavailable, match="binding changed"):
        service.credential("oidc-machine:bot")
    service.auth.machine_verifier.principal_allowed = lambda owner: False
    with pytest.raises(DelegationUnavailable, match="no longer authorized"):
        service.credential("oidc-machine:bot")


def test_local_token_never_creates_delegation(broker):
    service, _, requests = broker
    service.capture_bearer("locally-signed-session-token")
    assert requests == []


def test_revoked_refresh_fails_without_fallback(broker):
    service, _, requests = broker
    service.capture("alice@example.com", "person-sub", "original")
    connection = service.store.get("alice@example.com", "oidc_mcp", with_secret=True)
    service.store.update_secret(
        "alice@example.com", "oidc_mcp", secret={**connection.secret, "expires_at": 0}
    )
    service.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    with pytest.raises(DelegationUnavailable):
        service.credential("alice@example.com")
    assert len(requests) == 1


def test_refresh_grant_required(broker):
    service, _, _ = broker
    service.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"access_token": "access", "expires_in": 300, "token_type": "Bearer"}
            )
        )
    )
    with pytest.raises(DelegationUnavailable, match="renewable"):
        service.capture("alice@example.com", "person-sub", "original")


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com",
        "https://user:pass@mcp.example.com",
        "https://mcp.example.com/#x",
        "https://mcp.example.com/?target=x",
    ],
)
def test_config_rejects_unsafe_upstream(monkeypatch, url):
    monkeypatch.setenv("TEST_EXCHANGE_SECRET", "secret")
    with pytest.raises(ValueError):
        MCPDelegationConfig.parse(
            {
                "upstream_url": url,
                "audience": "mcp-api",
                "scope": "mcp-api",
                "machine_client_id": "exchange",
                "machine_client_secret_env": "TEST_EXCHANGE_SECRET",
            }
        )


@pytest.mark.asyncio
async def test_relay_binds_upstream_session_to_host_owner(broker, monkeypatch):
    from fastapi import FastAPI

    from omnigent.server.routes.mcp_identity import create_mcp_identity_router

    service, _, _ = broker
    service.oidc.cookie_secret = b"fixture-cookie-secret"
    service.capture("alice@example.com", "person-sub", "original")
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
            headers={"mcp-session-id": "upstream-session"},
        )

    original_client = httpx.AsyncClient
    app = FastAPI()
    hosts = SimpleNamespace(
        resolve_launch_token=lambda host, token: (
            SimpleNamespace(user_id="alice@example.com") if token == "host-secret" else None
        )
    )
    app.include_router(create_mcp_identity_router(hosts, service))
    async with original_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        monkeypatch.setattr(
            "omnigent.server.routes.mcp_identity.httpx.AsyncClient",
            lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
        )
        denied = await client.post("/hosts/one/mcp")
        assert denied.status_code == 401
        headers = {
            "X-Omnigent-Host-Token": "host-secret",
            "Authorization": "Bearer attacker",
            "Cookie": "private",
        }
        response = await client.post(
            "/hosts/one/mcp",
            headers=headers,
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
        )
        assert response.status_code == 200
        bound = response.headers["mcp-session-id"]
        assert bound != "upstream-session"
        assert seen[0].headers["authorization"] == "Bearer access"
        assert "cookie" not in seen[0].headers
        assert "x-omnigent-host-token" not in seen[0].headers
        headers["mcp-session-id"] = bound
        cross_host = await client.post("/hosts/two/mcp", headers=headers)
        assert cross_host.status_code == 403
        assert len(seen) == 1
        same_host = await client.post("/hosts/one/mcp", headers=headers)
        assert same_host.status_code == 200
        assert seen[-1].headers["mcp-session-id"] == "upstream-session"


def test_concurrent_refresh_uses_one_rotation(broker):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, _, requests = broker
    service.capture("alice@example.com", "person-sub", "original")
    connection = service.store.get("alice@example.com", "oidc_mcp", with_secret=True)
    service.store.update_secret(
        "alice@example.com", "oidc_mcp", secret={**connection.secret, "expires_at": 0}
    )
    barrier = Barrier(2)

    def resolve():
        barrier.wait(timeout=5)
        return service.credential("alice@example.com")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resolve), pool.submit(resolve)]
        assert [f.result(timeout=10) for f in futures] == ["access", "access"]
    assert len(requests) == 2
