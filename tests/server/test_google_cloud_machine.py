"""Machine policy, session isolation and token lifecycle through the host API."""

import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request

from omnigent.db.db_models import SqlConversationMetadata, SqlHost, SqlSessionPermission
from omnigent.db.utils import get_or_create_engine
from omnigent.server.google_cloud_approval import preflight_google_cloud
from omnigent.server.google_cloud_machine import (
    GoogleCloudMachineBroker,
    MachineCloudBinding,
    parse_machine_cloud_bindings,
)
from omnigent.server.routes.google_cloud_machine import create_machine_cloud_router
from omnigent.server.routes.host_credentials import create_host_credentials_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore, hash_host_launch_token
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_google_cloud_session_access import UserAuth


class MachineAuth(UserAuth):
    def get_machine_credential_user_id(self, request):
        return request.headers.get("X-Test-User")


BOT = "oidc-machine:test-bot"
ACCOUNT = "test-bot@example-project.iam.gserviceaccount.com"


@pytest.fixture
def setup(db_uri):
    sid, hid = uuid.uuid4().hex, uuid.uuid4().hex
    SqlAlchemyConversationStore(db_uri).create_conversation(
        conversation_id=sid, host_id=hid, workspace="/workspace"
    )
    with Session(get_or_create_engine(db_uri)) as db:
        db.add(
            SqlHost(
                host_id=hid,
                user_id=BOT,
                name="sandbox",
                status=1,
                created_at=1,
                updated_at=1,
                sandbox_provider="agent_sandbox",
                token_hash=hash_host_launch_token("launch"),
                token_expires_at=int(time.time()) + 3600,
            )
        )
        db.add(SqlSessionPermission(user_id=BOT, conversation_id=sid, level=4))
        db.add(SqlSessionPermission(user_id="reader@example.com", conversation_id=sid, level=1))
        db.commit()
    enabled = {BOT}
    broker = GoogleCloudMachineBroker(
        db_uri, {BOT: MachineCloudBinding(ACCOUNT)}, lambda p: p in enabled
    )
    broker._mint = AsyncMock(return_value=("machine-token", time.time() + 600))
    app = FastAPI()
    app.state.google_cloud_machine_broker = broker
    app.include_router(create_host_credentials_router(HostStore(db_uri)), prefix="/v1")
    app.include_router(
        create_machine_cloud_router(
            broker,
            MachineAuth(),
            SqlAlchemyPermissionStore(db_uri),
            SqlAlchemyConversationStore(db_uri),
        ),
        prefix="/v1",
    )
    return SimpleNamespace(
        broker=broker,
        app=app,
        client=TestClient(app),
        session=sid,
        host=hid,
        uri=db_uri,
        enabled=enabled,
    )


def vend(s, token="launch"):
    return s.client.get(
        f"/v1/hosts/{s.host}/credentials/google_cloud", headers={"X-Omnigent-Host-Token": token}
    )


def test_opt_in_and_reader_status(setup):
    s = setup
    assert vend(s, "wrong").status_code == 401
    assert vend(s).json()["reason"] == "machine_access_denied"
    path = f"/v1/sessions/{s.session}/google-cloud"
    assert (
        s.client.post(
            path, headers={"X-Test-User": "reader@example.com"}, json={"enabled": True}
        ).status_code
        == 403
    )
    assert (
        s.client.post(path, headers={"X-Test-User": BOT}, json={"enabled": True}).status_code
        == 200
    )
    assert vend(s).json()["email"] == ACCOUNT
    assert vend(s).json()["token"] == "machine-token"
    assert s.broker._mint.await_count == 1
    status = s.client.get(path, headers={"X-Test-User": "reader@example.com"})
    assert status.status_code == 200, status.text
    assert status.json()["authorization"] == "operator"
    assert "token" not in status.json()
    assert s.client.get(path, headers={"X-Test-User": "stranger@example.com"}).status_code == 403
    assert (
        s.client.post(path, headers={"X-Test-User": BOT}, json={"enabled": False}).status_code
        == 200
    )
    assert vend(s).json()["connected"] is False


@pytest.mark.parametrize(
    "change", ["binding", "principal", "owner", "host_owner", "deleted", "shared", "account"]
)
def test_revocation_and_host_isolation(setup, change):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)
    assert vend(s).json()["connected"]
    with Session(get_or_create_engine(s.uri)) as db:
        if change == "binding":
            s.broker.bindings.clear()
        elif change == "principal":
            s.enabled.clear()
        elif change == "owner":
            db.get(SqlSessionPermission, (0, BOT, s.session)).level = 2
        elif change == "host_owner":
            db.get(SqlHost, (0, s.host)).user_id = "oidc-machine:other"
        elif change == "deleted":
            db.get(SqlHost, (0, s.host)).deleted_at = 1
        elif change == "shared":
            db.add(
                SqlConversationMetadata(
                    id=uuid.uuid4().hex, host_id=s.host, workspace="/workspace", kind=1
                )
            )
        elif change == "account":
            s.broker.bindings[BOT] = MachineCloudBinding(
                "other-bot@example-project.iam.gserviceaccount.com"
            )
        db.commit()
    result = vend(s)
    assert result.status_code == 401 or result.json()["connected"] is False


@pytest.mark.asyncio
async def test_preflight_refresh_and_restart(setup):
    s = setup
    request = Request({"type": "http", "app": s.app})
    command = {
        "name": "bash",
        "arguments": {"command": "gcloud projects describe example-project"},
    }
    assert await preflight_google_cloud(request, s.host, command) is not None
    s.broker.session(s.session, BOT, enabled=True)
    assert await preflight_google_cloud(request, s.host, command) is None
    await s.broker.credential(s.host, BOT)
    s.broker._tokens.clear()
    await s.broker.credential(s.host, BOT)
    assert s.broker._mint.await_count == 2
    restarted = GoogleCloudMachineBroker(s.uri, s.broker.bindings, lambda p: p in s.enabled)
    restarted._mint = AsyncMock(return_value=("renewed", time.time() + 600))
    assert (await restarted.credential(s.host, BOT))["token"] == "renewed"


@pytest.mark.asyncio
async def test_revoke_while_minting(setup):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)

    async def mint(account):
        s.enabled.clear()
        return "never-release", time.time() + 600

    s.broker._mint = mint
    with pytest.raises(PermissionError):
        await s.broker.credential(s.host, BOT)


@pytest.mark.asyncio
async def test_gke_token_exchange(setup):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "source-token"})
        assert request.url.host == "iamcredentials.googleapis.com"
        assert request.headers["Authorization"] == "Bearer source-token"
        assert json.loads(request.content) == {
            "scope": ["https://www.googleapis.com/auth/cloud-platform"],
            "lifetime": "600s",
        }
        return httpx.Response(
            200,
            json={
                "accessToken": "short-token",
                "expireTime": datetime.fromtimestamp(time.time() + 600, UTC).isoformat(),
            },
        )

    broker = GoogleCloudMachineBroker(
        setup.uri, setup.broker.bindings, lambda _: True, transport=httpx.MockTransport(handle)
    )
    token, expiry = await broker._mint(ACCOUNT)
    assert token == "short-token" and expiry > time.time()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "value",
    [
        {"human@example.com": {"service_account": ACCOUNT}},
        {BOT: {"service_account": "https://evil.example"}},
        {BOT: {"service_account": ACCOUNT, "scopes": []}},
    ],
)
def test_invalid_config(value):
    with pytest.raises(ValueError):
        parse_machine_cloud_bindings(value, principals=frozenset({BOT}))


@pytest.mark.asyncio
async def test_changed_account_cannot_reuse_cached_token(setup):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)
    await s.broker.credential(s.host, BOT)
    s.broker.bindings[BOT] = MachineCloudBinding(
        "second-bot@example-project.iam.gserviceaccount.com"
    )
    s.broker.session(s.session, BOT, enabled=True)
    s.broker._mint = AsyncMock(return_value=("new-account-token", time.time() + 600))
    result = await s.broker.credential(s.host, BOT)
    assert result["token"] == "new-account-token"
    assert result["email"].startswith("second-bot@")


@pytest.mark.asyncio
async def test_wake_with_replacement_host_retains_opt_in(setup):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)
    new_host = uuid.uuid4().hex
    with Session(get_or_create_engine(s.uri)) as db:
        db.get(SqlHost, (0, s.host)).deleted_at = 1
        db.add(
            SqlHost(
                host_id=new_host,
                user_id=BOT,
                name="woken",
                status=1,
                created_at=1,
                updated_at=1,
                sandbox_provider="agent_sandbox",
            )
        )
        db.get(SqlConversationMetadata, (0, s.session)).host_id = new_host
        db.commit()
    with pytest.raises(PermissionError):
        await s.broker.credential(s.host, BOT)
    assert (await s.broker.credential(new_host, BOT))["token"] == "machine-token"


@pytest.mark.asyncio
async def test_expired_cache_refreshes_and_provider_failure_is_sanitized(setup):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)
    await s.broker.credential(s.host, BOT)
    key = (BOT, s.broker.bindings[BOT].generation)
    s.broker._tokens[key] = ("expired", time.time() - 1)
    s.broker._mint = AsyncMock(return_value=("refreshed", time.time() + 600))
    assert (await s.broker.credential(s.host, BOT))["token"] == "refreshed"
    s.broker._tokens.clear()
    s.broker._mint = AsyncMock(side_effect=RuntimeError("sensitive-upstream-response"))
    response = vend(s)
    assert response.status_code == 503
    assert "sensitive" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_fork_does_not_inherit_machine_opt_in(setup):
    s = setup
    s.broker.session(s.session, BOT, enabled=True)
    fork = SqlAlchemyConversationStore(s.uri).fork_conversation(s.session)
    SqlAlchemyPermissionStore(s.uri).grant(BOT, fork.id, 4)
    assert s.broker.session(fork.id, BOT)["state"] == "off"


@pytest.mark.parametrize(
    "host_state", ["hostless", "missing", "external", "deleted", "wrong_owner", "shared"]
)
def test_explicit_decision_requires_managed_root(setup, host_state):
    s = setup
    with Session(get_or_create_engine(s.uri)) as db:
        host = db.get(SqlHost, (0, s.host))
        root = db.get(SqlConversationMetadata, (0, s.session))
        if host_state == "hostless":
            root.host_id = None
        elif host_state == "missing":
            root.host_id = uuid.uuid4().hex
        elif host_state == "external":
            host.sandbox_provider = None
        elif host_state == "deleted":
            host.deleted_at = 1
        elif host_state == "wrong_owner":
            host.user_id = "oidc-machine:other"
        elif host_state == "shared":
            db.add(
                SqlConversationMetadata(
                    id=uuid.uuid4().hex, host_id=s.host, workspace="/workspace", kind=1
                )
            )
        db.commit()
    result = s.client.post(
        f"/v1/sessions/{s.session}/google-cloud",
        json={"enabled": True},
        headers={"X-Test-User": BOT},
    )
    assert result.status_code == 403
    assert s.broker.session(s.session, BOT)["state"] == "off"
