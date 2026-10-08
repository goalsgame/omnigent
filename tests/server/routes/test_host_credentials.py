"""Tests for the host-facing, provider-generic credential endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.routes.host_credentials import create_host_credentials_router


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


@dataclass
class _Managed:
    user_id: str


class _FakeHostStore:
    """Resolves a single (host_id, token) pair to an owner."""

    def __init__(self, host_id: str, token: str, owner: str) -> None:
        self._host_id, self._token, self._owner = host_id, token, owner

    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        if host_id == self._host_id and token == self._token:
            return _Managed(self._owner)
        return None


class _BoomStore:
    """A connection store whose reads raise — to prove the route degrades."""

    def get(self, *args, **kwargs):
        raise RuntimeError("db down")


def _app(
    host_store: _FakeHostStore,
    *,
    github_store,
    github_client=None,
    github_machine_broker=None,
    openrouter_wif_broker=None,
) -> TestClient:
    app = FastAPI()
    # The generic route reads app.state.<provider>_{store,client}, populated by
    # the connection-provider wiring in create_app.
    app.state.github_store = github_store
    app.state.github_client = github_client
    app.include_router(
        create_host_credentials_router(
            host_store,
            github_machine_broker=github_machine_broker,
            openrouter_wif_broker=openrouter_wif_broker,
        ),
        prefix="/v1",
    )  # type: ignore[arg-type]
    return TestClient(app)


_HDR = {MANAGED_HOST_TOKEN_HEADER: "launch-tok"}


def test_returns_github_credential_for_valid_host_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_live", "ghr_x", None, None, "repo"),
    )
    tc = _app(hs, github_store=store)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.json() == {
        "connected": True,
        "owner": "alice@example.com",
        "username": "x-access-token",
        "token": "ghu_live",
        "login": "octocat",
    }


def test_unauthenticated_without_or_with_bad_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    tc = _app(hs, github_store=store)
    assert tc.get("/v1/hosts/host1/credentials/github").status_code == 401
    bad = tc.get("/v1/hosts/host1/credentials/github", headers={MANAGED_HOST_TOKEN_HEADER: "nope"})
    assert bad.status_code == 401
    # Right token but wrong host id → also fails closed.
    assert tc.get("/v1/hosts/other/credentials/github", headers=_HDR).status_code == 401


def test_connected_false_when_owner_has_no_github(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))  # no upsert
    tc = _app(hs, github_store=store)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False, "reason": "not_connected"}


def test_unknown_provider_is_404_but_only_after_auth(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    tc = _app(hs, github_store=store)
    # Authenticated, but no resolver/store registered for 'gitlab'.
    assert tc.get("/v1/hosts/host1/credentials/gitlab", headers=_HDR).status_code == 404
    # Unauthenticated stays 401 even for an unknown provider — auth is checked
    # first, so the endpoint reveals nothing about which providers exist.
    assert tc.get("/v1/hosts/host1/credentials/gitlab").status_code == 401


def test_resolver_fault_reports_unavailable() -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc = _app(hs, github_store=_BoomStore())
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"detail": "Provider credential unavailable"}


def test_machine_never_falls_back_to_a_user_connection(db_uri: str) -> None:
    principal = "oidc-machine:ticket-bot"
    hs = _FakeHostStore("host1", "launch-tok", principal)
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    store.upsert(
        principal,
        github_login="human",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_human", None, None, None, "repo"),
    )
    tc = _app(hs, github_store=store)
    assert tc.get("/v1/hosts/host1/credentials/github", headers=_HDR).json() == {
        "connected": False
    }


def test_machine_token_is_bound_to_authenticated_host_owner() -> None:
    calls = []

    class Broker:
        async def resolve(self, principal):
            calls.append(principal)
            return {"username": "x-access-token", "token": "ghs_bot"}

    hs = _FakeHostStore("host1", "launch-tok", "oidc-machine:ticket-bot")
    tc = _app(hs, github_store=None, github_machine_broker=Broker())
    assert tc.get("/v1/hosts/host1/credentials/github").status_code == 401
    assert not calls
    response = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert response.json()["token"] == "ghs_bot"
    assert response.json()["owner"] == "oidc-machine:ticket-bot"
    assert response.headers["cache-control"] == "no-store"
    assert calls == ["oidc-machine:ticket-bot"]


def test_openrouter_broker_authenticates_each_fetch_and_stops_after_revocation():
    calls = []

    class Broker:
        async def credential(self):
            calls.append(True)
            return {"token": "short-lived", "expires_in": 800}

    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc = _app(hs, github_store=None, openrouter_wif_broker=Broker())
    endpoint = "/v1/hosts/host1/credentials/openrouter"
    assert tc.get(endpoint).status_code == 401
    assert tc.get("/v1/hosts/other/credentials/openrouter", headers=_HDR).status_code == 401
    assert not calls
    response = tc.get(endpoint, headers=_HDR)
    assert response.json() == {"connected": True, "token": "short-lived", "expires_in": 800}
    assert response.headers["cache-control"] == "no-store"
    hs._token = "revoked"
    assert tc.get(endpoint, headers=_HDR).status_code == 401
    assert len(calls) == 1


def test_openrouter_exchange_failure_and_disabled_broker():
    from omnigent.server.openrouter_wif import OpenRouterWIFError

    class Broker:
        async def credential(self):
            raise OpenRouterWIFError("exchange failed")

    hs = _FakeHostStore("host1", "launch-tok", "oidc-machine:ticket-bot")
    endpoint = "/v1/hosts/host1/credentials/openrouter"
    assert _app(hs, github_store=None).get(endpoint, headers=_HDR).status_code == 404
    tc = _app(hs, github_store=None, openrouter_wif_broker=Broker())
    response = tc.get(endpoint, headers=_HDR)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "token" not in response.json()


@pytest.mark.parametrize("expired", [True, False])
def test_linked_account_refresh_failure_does_not_report_missing_connection(
    db_uri: str, expired: bool
) -> None:
    owner = "alice@example.com"
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    store.upsert(
        owner,
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet(
            "ghu_existing", "ghr_existing", now_epoch() + (-60 if expired else 120), None, "repo"
        ),
    )
    client = AsyncMock()
    client.refresh_token.side_effect = httpx.TimeoutException("refresh unavailable")
    tc = _app(
        _FakeHostStore("host1", "launch-tok", owner), github_store=store, github_client=client
    )
    response = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert response.status_code == (503 if expired else 200)
    assert response.headers["cache-control"] == "no-store"
    client.refresh_token.assert_awaited_once_with("ghr_existing")
    assert "reason" not in response.json()
    if expired:
        assert response.json() == {"detail": "Provider credential unavailable"}
    else:
        assert response.json()["token"] == "ghu_existing"
    assert store.get(owner) is not None
