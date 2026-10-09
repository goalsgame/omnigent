"""Session consent gates the real managed-host credential endpoint."""

import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from omnigent.connections.google_cloud import GoogleCloudConnectionStore
from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlHost,
    SqlSessionPermission,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, UnifiedAuthProvider
from omnigent.server.google_cloud import SCOPES, GoogleCloudConfig
from omnigent.server.routes.connections_google_cloud import create_connections_google_cloud_router
from omnigent.server.routes.host_credentials import create_host_credentials_router
from omnigent.stores.host_store import HostStore, hash_host_launch_token
from tests.server.test_github_store import SecretBox

OWNER = "owner@example.com"


class UserAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("X-Test-User")

    def get_credential_user_id(self, request):
        return request.headers.get("X-Test-User")


@pytest.fixture
def setup(db_uri, request):
    local = getattr(request, "param", False)
    owner = RESERVED_USER_LOCAL if local else OWNER
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    store = GoogleCloudConnectionStore(db_uri, SecretBox("test-key"))
    with Session(get_or_create_engine(db_uri)) as db:
        db.add(
            SqlHost(
                host_id=host_id,
                user_id=owner,
                name="sandbox",
                status=1,
                created_at=1,
                updated_at=1,
                sandbox_provider="agent_sandbox",
                token_hash=hash_host_launch_token("launch"),
                token_expires_at=int(time.time()) + 3600,
            )
        )
        db.add(
            SqlConversationMetadata(id=session_id, host_id=host_id, workspace="/workspace", kind=1)
        )
        db.add(SqlSessionPermission(user_id=owner, conversation_id=session_id, level=4))
        db.commit()

    def connect():
        store.upsert(
            owner,
            tokens={
                "access_token": "access-one",
                "refresh_token": "refresh",
                "expires_at": time.time() + 3600,
                "scope": SCOPES,
            },
            identity={"subject": "google-user", "email": OWNER},
        )

    connect()
    app = FastAPI()
    app.state.google_cloud_store = store
    app.state.google_cloud_client = SimpleNamespace(token=AsyncMock())
    app.include_router(
        create_connections_google_cloud_router(
            GoogleCloudConfig("fixture", "secret", "https://app.example/callback"),
            store,
            auth_provider=(
                UnifiedAuthProvider(source="header", local_single_user=True)
                if local == "header"
                else None
                if local
                else UserAuth()
            ),
        ),
        prefix="/v1",
    )
    app.include_router(create_host_credentials_router(HostStore(db_uri)), prefix="/v1")
    return SimpleNamespace(
        store=store,
        client=TestClient(app),
        session=session_id,
        host=host_id,
        uri=db_uri,
        connect=connect,
    )


def access(s, method="get", user=OWNER, **kwargs):
    return getattr(s.client, method)(
        f"/v1/connections/google_cloud/sessions/{s.session}/access",
        headers={"X-Test-User": user},
        **kwargs,
    )


def credential(s):
    return s.client.get(
        f"/v1/hosts/{s.host}/credentials/google_cloud", headers={"X-Omnigent-Host-Token": "launch"}
    )


def approve(s):
    status = access(s).json()
    return access(s, "post", json={"decision": "allowed", "generation": status["generation"]})


def test_first_use_prompts_then_approval_vends_and_revocation_blocks(setup):
    s = setup
    assert access(s).json()["state"] == "off"
    blocked = credential(s)
    assert blocked.json() == {"connected": False, "reason": "session_access_pending"}
    assert access(s).json()["state"] == "pending"
    assert approve(s).status_code == 200
    assert credential(s).json()["token"] == "access-one"
    generation = access(s).json()["generation"]
    assert (
        access(s, "post", json={"decision": "denied", "generation": generation}).status_code == 200
    )
    assert credential(s).json()["reason"] == "session_access_denied"
    assert access(s).json()["state"] == "denied"


@pytest.mark.parametrize("level", [1, 2, 3])
def test_collaborators_cannot_grant_or_read_consent(setup, level):
    s = setup
    with Session(get_or_create_engine(s.uri)) as db:
        db.add(
            SqlSessionPermission(
                user_id="other@example.com", conversation_id=s.session, level=level
            )
        )
        db.commit()
    assert access(s, user="other@example.com").status_code == 403
    assert (
        access(
            s,
            "post",
            user="other@example.com",
            json={"decision": "allowed", "generation": "forged"},
        ).status_code
        == 403
    )
    assert credential(s).json()["connected"] is False


def test_reconnect_requires_fresh_consent_and_rejects_stale_approval(setup):
    s = setup
    old = approve(s).json()["generation"]
    s.connect()
    assert credential(s).json()["connected"] is False
    assert access(s, "post", json={"decision": "allowed", "generation": old}).status_code == 409
    assert approve(s).status_code == 200
    assert credential(s).json()["connected"] is True
    s.store.delete(OWNER)
    assert credential(s).json()["reason"] == "session_access_not_connected"


def test_other_session_and_ambiguous_host_never_inherit_consent(setup):
    s = setup
    approve(s)
    with Session(get_or_create_engine(s.uri)) as db:
        db.add(
            SqlConversationMetadata(
                id=uuid.uuid4().hex, host_id=s.host, workspace="/another", kind=1
            )
        )
        db.commit()
    assert credential(s).json()["reason"] == "session_access_unavailable"


def test_host_token_cannot_approve_and_machine_cannot_approve(setup):
    s = setup
    with pytest.raises(OmnigentError, match="Authentication required"):
        s.client.post(
            f"/v1/connections/google_cloud/sessions/{s.session}/access",
            headers={"X-Omnigent-Host-Token": "launch"},
            json={"decision": "allowed", "generation": "x"},
        )
    assert (
        access(
            s, "post", user="oidc-machine:bot", json={"decision": "allowed", "generation": "x"}
        ).status_code
        == 403
    )


def test_scope_is_workspace_local(setup):
    s = setup
    approve(s)
    with workspace_scope(99):
        assert s.store.access.host(s.host, OWNER) == "unavailable"


def test_broker_rechecks_revocation_during_resolution(setup, monkeypatch):
    s = setup
    approve(s)
    original = s.store.get

    def revoke_on_decrypt(user_id, *, with_tokens=False):
        result = original(user_id, with_tokens=with_tokens)
        if with_tokens:
            s.store.access.session(
                s.session, OWNER, decision="denied", generation=result.metadata["generation"]
            )
        return result

    monkeypatch.setattr(s.store, "get", revoke_on_decrypt)
    assert credential(s).json() == {"connected": False, "reason": "session_access_denied"}


def test_reapproval_of_new_account_cannot_release_old_account_token(setup, monkeypatch):
    s = setup
    approve(s)
    original = s.store.access.host

    def reconnect_before_final_check(host_id, user_id, **kwargs):
        if kwargs.get("expected_generation"):
            s.connect()
            current = s.store.access.session(s.session, OWNER)
            s.store.access.session(
                s.session, OWNER, decision="allowed", generation=current["generation"]
            )
        return original(host_id, user_id, **kwargs)

    monkeypatch.setattr(s.store.access, "host", reconnect_before_final_check)
    assert credential(s).json() == {"connected": False, "reason": "session_access_off"}


@pytest.mark.parametrize("setup", [True, "header"], indirect=True)
@pytest.mark.parametrize("headers", [{}, {"X-Omnigent-Host-Token": "launch"}])
def test_local_sandbox_cannot_self_approve_or_reuse_a_saved_grant(setup, headers):
    s = setup
    connection = s.store.get(RESERVED_USER_LOCAL)
    generation = connection.metadata["generation"]
    endpoint = f"/v1/connections/google_cloud/sessions/{s.session}/access"
    assert s.client.get(endpoint, headers=headers).status_code == 403
    denied = s.client.post(
        endpoint, headers=headers, json={"decision": "allowed", "generation": generation}
    )
    assert denied.status_code == 403
    assert "requires server authentication" in denied.json()["detail"]
    with Session(get_or_create_engine(s.uri)) as db:
        row = db.get(SqlConversationMetadata, (current_workspace_id(), s.session))
        assert row is not None
        row.google_cloud_access = json.dumps(
            {
                "user_id": RESERVED_USER_LOCAL,
                "host_id": s.host,
                "generation": generation,
                "state": "allowed",
            }
        )
        db.commit()
    with pytest.raises(PermissionError, match="requires server authentication"):
        s.store.access.session(
            s.session, RESERVED_USER_LOCAL, decision="allowed", generation=generation
        )
    assert credential(s).json() == {"connected": False, "reason": "session_access_unavailable"}
