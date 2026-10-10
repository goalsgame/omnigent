"""Verified, expiring OIDC memberships never become user or machine identities."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import (
    access_principals,
    bind_group_authority,
    clear_group_authority,
    group_authority_scope,
    group_name,
    group_principal,
    verified_groups,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret
from omnigent.server.oidc import mint_session_token
from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID, create_oauth_token_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_oidc_human_auth import human, human_token
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    connection,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

__all__ = ["human", "machine_config", "oidc", "signing_key", "verifier"]


@pytest.fixture(autouse=True)
def clear_groups():
    clear_group_authority()
    yield
    clear_group_authority()


@pytest.mark.parametrize(
    "value", [None, "engineering", [1], [""], [" engineering"], ["a" * 88], ["a\n"], ["a"] * 257]
)
def test_malformed_claims_fail_closed(value):
    assert verified_groups(value) == ()


def test_exact_names_and_tenant_subject_expiry_boundaries():
    group = group_principal("/engineering")
    assert group_name(group) == "/engineering"
    assert group_principal("engineering") != group
    assert group_name("oidc-group:not!valid") is None
    bind_group_authority("member", ["/engineering"], int(time.time()) + 300)
    assert access_principals("member") == ("member", group)
    assert access_principals("other") == ("other",)
    with workspace_scope(42):
        assert access_principals("member") == ("member",)
    bind_group_authority("member", ["/engineering"], int(time.time()) - 1)
    assert access_principals("member") == ("member",)


def test_cookie_cache_does_not_replay_memberships_and_scope_checks_remain(oidc):
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc)
    deadline = int(time.time()) + 300
    token = mint_session_token(
        "member",
        oidc.cookie_secret,
        300,
        "oidc",
        group_authority={"groups": ["engineering"], "expires_at": deadline},
    )
    for websocket in (False, True):
        assert (
            provider.get_user_id(connection(token, cookie=True, websocket=websocket)) == "member"
        )
        assert len(access_principals("member")) == 2
    assert not provider._cookie_cache
    plain = mint_session_token("other", oidc.cookie_secret, 300, "oidc")
    assert provider.get_user_id(connection(plain)) == "other"
    assert access_principals("member") == ("member",)
    payload = jwt.decode(token, oidc.cookie_secret, algorithms=["HS256"])
    payload.update(scope="delegated", grant_id="revoked")
    restricted = jwt.encode(payload, oidc.cookie_secret, algorithm="HS256")
    provider.set_grant_revocation_check(lambda grant: True)
    assert provider.get_user_id(connection(restricted)) is None
    assert access_principals("member") == ("member",)
    provider.set_grant_revocation_check(lambda grant: False)
    assert provider.get_user_id(connection(restricted, "/v1/me")) is None
    assert provider.get_user_id(connection(restricted)) == "member"
    assert len(access_principals("member")) == 2


def test_verified_connector_groups_and_machine_exclusion(human, oidc, verifier, signing_key):
    provider = UnifiedAuthProvider(
        "oidc", oidc_config=oidc, human_verifier=human, machine_verifier=verifier
    )
    human_bearer = human_token(signing_key, groups=["engineering"])
    assert provider.get_user_id(connection(human_bearer)) == "person@example.test"
    assert len(access_principals("person@example.test")) == 2
    assert (
        provider.get_user_id(connection(signed_token(signing_key, groups=["engineering"])))
        == PRINCIPAL
    )
    assert access_principals(PRINCIPAL) == (PRINCIPAL,)
    assert access_principals("person@example.test") == ("person@example.test",)
    impersonation = mint_session_token(
        group_principal("engineering"), oidc.cookie_secret, 300, "oidc"
    )
    assert provider.get_user_id(connection(impersonation)) is None


def test_group_acl_max_level_cache_revocation_and_owner_guard(db_uri):
    store = SqlAlchemyPermissionStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    session_id = conversations.create_conversation().id
    store.ensure_user("member")
    store.grant("member", session_id, 1)
    group = group_principal("engineering")
    store.grant(group, session_id, 3)
    bind_group_authority("member", ["engineering"], int(time.time()) + 300)
    assert store.check_access("member", session_id, 3)
    assert not store.check_access("member", session_id, 4)
    assert store.get_permission_level("member", session_id) == 3
    assert store.resolve_access("member", session_id).user_grant_level == 3
    clear_group_authority()
    assert store.resolve_access("member", session_id).user_grant_level == 1
    bind_group_authority("member", ["engineering"], int(time.time()) + 300)
    store.revoke(group, session_id)
    assert store.resolve_access("member", session_id).user_grant_level == 1
    with pytest.raises(ValueError, match="cannot own"):
        store.grant(group, session_id, 4)
    with pytest.raises(ValueError, match="cannot become"):
        store.ensure_user(group)
    assert store.get_user(group) is None


def test_refresh_keeps_original_group_membership_deadline(db_uri, oidc):
    store = DeviceGrantStore(db_uri)
    deadline = int(time.time()) + 60
    snapshot = {"groups": ["engineering"], "expires_at": deadline}
    store.create_redeemed_grant(
        "grant",
        user_id="member",
        client_id=LOGIN_GRANT_CLIENT_ID,
        refresh_token_hash=hash_secret("refresh", oidc.cookie_secret),
        created_at=int(time.time()),
        group_authority=snapshot,
    )
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc)
    app = FastAPI()
    app.include_router(create_oauth_token_router(provider, store))
    with TestClient(app) as client:
        response = client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "refresh"}
        )
        assert response.status_code == 200, response.text
        claims = jwt.decode(
            response.json()["access_token"], oidc.cookie_secret, algorithms=["HS256"]
        )
        assert claims["group_authority"] == snapshot
        assert provider.get_user_id(connection(response.json()["access_token"])) == "member"
        assert len(access_principals("member")) == 2
        store.revoke("grant")
        provider.set_grant_revocation_check(store.is_revoked)
        assert provider.get_user_id(connection(response.json()["access_token"])) is None


def test_request_scope_does_not_inherit_group_authority():
    bind_group_authority("member", ["engineering"], int(time.time()) + 300)
    with group_authority_scope():
        assert access_principals("member") == ("member",)
    assert len(access_principals("member")) == 2
