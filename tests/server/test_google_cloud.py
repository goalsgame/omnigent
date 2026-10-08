"""Google grants stay owner-bound, encrypted, and refresh without resurrecting disconnects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from omnigent.connections.google_cloud import GoogleCloudConnectionStore
from omnigent.server.google_cloud import (
    SCOPES,
    GoogleCloudClient,
    GoogleCloudConfig,
    GoogleCloudError,
    resolve_google_cloud_credential,
)
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.server.routes.connections_google_cloud import GoogleCloudConnectionHooks
from omnigent.server.routes.host_credentials import create_host_credentials_router
from tests.server.test_github_store import SecretBox

USER = "alice@example.com"
CONFIG = GoogleCloudConfig(
    "client.apps.example",
    "client-secret",
    "https://app.example/v1/connections/google_cloud/callback",
)


def tokens(*, expired=False, token="access"):
    return {
        "access_token": token,
        "refresh_token": "refresh",
        "expires_at": time.time() + (-1 if expired else 3500),
        "scope": SCOPES,
    }


@pytest.fixture
def store(db_uri):
    return GoogleCloudConnectionStore(db_uri, SecretBox("key"))


def connect(store, **kwargs):
    store.upsert(USER, tokens=tokens(**kwargs), identity={"subject": "google-user", "email": USER})


@pytest.mark.asyncio
async def test_refresh_vends_only_access_token_and_preserves_refresh(store):
    connect(store, expired=True)
    client = SimpleNamespace(
        token=AsyncMock(return_value={"access_token": "fresh", "expires_at": time.time() + 3500})
    )
    result = await resolve_google_cloud_credential(USER, store=store, client=client)
    assert result["token"] == "fresh"
    assert "refresh_token" not in result
    assert store.get(USER).secret is None
    assert store.get(USER, with_tokens=True).secret["refresh_token"] == "refresh"
    await resolve_google_cloud_credential(USER, store=store, client=client)
    client.token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "reconnect"])
async def test_refresh_cannot_restore_old_connection(store, operation):
    connect(store, expired=True)

    async def refresh(_fields):
        if operation == "delete":
            store.delete(USER)
        else:
            connect(store, token="reconnected")
        return {"access_token": "stale-result", "expires_at": time.time() + 3500}

    result = await resolve_google_cloud_credential(
        USER, store=store, client=SimpleNamespace(token=refresh)
    )
    assert result is None
    row = store.get(USER, with_tokens=True)
    assert row is None if operation == "delete" else row.secret["access_token"] == "reconnected"


def test_compare_and_swap_rejects_reconnection_during_store_write(store):
    connect(store, expired=True)
    old = store.get(USER, with_tokens=True)
    connect(store, token="reconnected")
    assert not store.refresh(USER, tokens=tokens(token="stale-result"), connection=old)
    assert store.get(USER, with_tokens=True).secret["access_token"] == "reconnected"
    store.delete(USER)
    assert not store.refresh(USER, tokens=tokens(), connection=old)
    assert store.get(USER) is None


@pytest.mark.asyncio
async def test_unconnected_and_machine_users_do_not_inherit_access(store):
    connect(store)
    client = SimpleNamespace(token=AsyncMock())
    for user in ("bob@example.com", "oidc-machine:bot"):
        assert await resolve_google_cloud_credential(user, store=store, client=client) is None
    client.token.assert_not_awaited()


def test_oauth_uses_offline_consent_and_secret_derived_pkce(store):
    hooks = GoogleCloudConnectionHooks(CONFIG, store, GoogleCloudClient(CONFIG))
    claims = {}

    def state(value):
        claims.update(value)
        return "signed-state"

    start = hooks.begin(Request({"type": "http"}), state)
    query = parse_qs(urlsplit(start.authorize_url).query)
    assert query["access_type"] == ["offline"]
    assert "consent" in query["prompt"][0]
    assert query["scope"] == [SCOPES]
    verifier = hooks.verifier(claims["nonce"])
    assert verifier not in start.authorize_url
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["refresh_token", "scope"])
async def test_incomplete_google_grants_are_not_saved(store, missing):
    value = tokens()
    value.pop(missing)
    api = SimpleNamespace(token=AsyncMock(return_value=value), identity=AsyncMock())
    hooks = GoogleCloudConnectionHooks(CONFIG, store, api)
    with pytest.raises(ConnectionError):
        await hooks.complete(USER, "code", {"nonce": "nonce"})
    assert store.get(USER) is None
    api.identity.assert_not_awaited()


def test_host_broker_authenticates_owner_and_disconnects(store, monkeypatch):
    connect(store)
    # This test isolates provider resolution; real consent uses managed rows in its own suite.
    monkeypatch.setattr(store.access, "host", lambda *args, **kwargs: "allowed")

    class Hosts:
        def resolve_launch_token(self, host_id, token):
            return (
                SimpleNamespace(user_id=USER) if (host_id, token) == ("host", "launch") else None
            )

    app = FastAPI()
    app.state.google_cloud_store = store
    app.state.google_cloud_client = SimpleNamespace(token=AsyncMock())
    app.include_router(create_host_credentials_router(Hosts()), prefix="/v1")
    with TestClient(app) as client:
        path = "/v1/hosts/host/credentials/google_cloud"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Omnigent-Host-Token": "wrong"}).status_code == 401
        response = client.get(path, headers={"X-Omnigent-Host-Token": "launch"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["token"] == "access"
        assert "refresh" not in response.text
        store.delete(USER)
        assert (
            client.get(path, headers={"X-Omnigent-Host-Token": "launch"}).json()["connected"]
            is False
        )


@pytest.mark.asyncio
async def test_provider_error_is_sanitized(monkeypatch):
    async def post(self, *args, **kwargs):
        return httpx.Response(
            400,
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
            json={"error": "secret-must-not-leak"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(GoogleCloudError) as error:
        await GoogleCloudClient(CONFIG).token(
            {"grant_type": "refresh_token", "refresh_token": "very-secret"}
        )
    assert "very-secret" not in str(error.value)
    assert "secret-must-not-leak" not in str(error.value)


@pytest.mark.parametrize("flag", [None, "0", "false", "1"])
def test_oauth_configuration_requires_managed_host_opt_in(monkeypatch, flag):
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_CLIENT_ID", CONFIG.client_id)
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_CLIENT_SECRET", CONFIG.client_secret)
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_REDIRECT_URI", CONFIG.redirect_uri)
    if flag is None:
        monkeypatch.delenv("OMNIGENT_GOOGLE_CLOUD_AUTH", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_AUTH", flag)
    assert GoogleCloudConfig.from_env() == (CONFIG if flag == "1" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("loser_fails", [False, True])
@pytest.mark.parametrize("lifetime", [60, 3500])
async def test_concurrent_refresh_returns_winning_credential(store, loser_fails, lifetime):
    connect(store, expired=True)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def refresh(_fields):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await asyncio.wait_for(release[index].wait(), 5)
        if index == 1 and loser_fails:
            raise GoogleCloudError("refresh failed")
        return {"access_token": f"fresh-{index}", "expires_at": time.time() + lifetime}

    client = SimpleNamespace(token=refresh)
    first = asyncio.create_task(resolve_google_cloud_credential(USER, store=store, client=client))
    await asyncio.wait_for(entered[0].wait(), 5)
    second = asyncio.create_task(resolve_google_cloud_credential(USER, store=store, client=client))
    try:
        await asyncio.wait_for(entered[1].wait(), 5)
        release[0].set()
        winner = await asyncio.wait_for(first, 5)
        release[1].set()
        other = await asyncio.wait_for(second, 5)
        assert winner["token"] == other["token"] == "fresh-0"
        assert store.get(USER, with_tokens=True).secret["access_token"] == "fresh-0"
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
