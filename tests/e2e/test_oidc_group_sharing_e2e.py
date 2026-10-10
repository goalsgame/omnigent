"""Group sharing across authenticated HTTP clients and real SQL-backed routes."""

from __future__ import annotations

import time

import httpx
import pytest

from omnigent.db.group_authority import group_principal
from omnigent.server.oidc import mint_session_token
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import mock_llm, runtime_init
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_oidc_machine_auth import machine_app
from tests.server.test_oidc_human_auth import human_token
from tests.server.test_oidc_machine_auth import machine_config, oidc, signing_key, verifier

__all__ = [
    "machine_app",
    "machine_config",
    "mock_llm",
    "oidc",
    "runtime_init",
    "signing_key",
    "verifier",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize("client_kind", ["browser", "connector"])
async def test_group_grant_list_access_manage_and_revoke(
    machine_app, oidc, signing_key, level, client_kind
):
    owner = {
        "Authorization": "Bearer "
        + mint_session_token("owner@example.test", oidc.cookie_secret, 300, "oidc")
    }
    member_token = (
        human_token(signing_key, groups=["/engineering"])
        if client_kind == "connector"
        else mint_session_token(
            "person@example.test",
            oidc.cookie_secret,
            300,
            "oidc",
            group_authority={"groups": ["/engineering"], "expires_at": int(time.time()) + 300},
        )
    )
    member = (
        {"Authorization": "Bearer " + member_token}
        if client_kind == "connector"
        else {"Cookie": "__Host-ap_session=" + member_token}
    )
    nonmember = {
        "Authorization": "Bearer "
        + mint_session_token("outsider@example.test", oidc.cookie_secret, 300, "oidc")
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=machine_app), base_url="https://app.example.test"
    ) as client:
        created = await client.post(
            "/v1/sessions",
            headers=owner,
            data={"metadata": "{}"},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="shared-agent"),
                    "application/gzip",
                )
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        path = f"/v1/sessions/{session_id}"
        assert (await client.get(path, headers=member)).status_code == 404
        invalid = await client.put(
            path + "/permissions",
            headers={**owner, "Content-Type": "application/json"},
            content=b'{"principal_type":"group","user_id":"\\ud800","level":1}',
        )
        assert invalid.status_code == 400, invalid.text
        shared = await client.put(
            path + "/permissions",
            headers=owner,
            json={"principal_type": "group", "user_id": "/engineering", "level": level},
        )
        assert shared.status_code == 200, shared.text
        principal = shared.json()["user_id"]
        assert shared.json()["group_name"] == "/engineering"
        snapshot = await client.get(path, headers=member)
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["permission_level"] == level
        assert (await client.get(path, headers=nonmember)).status_code == 404
        listed = await client.get("/v1/sessions?visibility=shared", headers=member)
        assert listed.status_code == 200, listed.text
        assert any(
            item["id"] == session_id and item["permission_level"] == level
            for item in listed.json()["data"]
        )
        owned = await client.get("/v1/sessions?visibility=mine", headers=member)
        assert owned.status_code == 200, owned.text
        assert all(item["id"] != session_id for item in owned.json()["data"])
        edited = await client.patch(path, headers=member, json={"title": "Shared work"})
        assert edited.status_code == (200 if level >= 2 else 403), edited.text
        managed = await client.get(path + "/permissions", headers=member)
        assert managed.status_code == (200 if level >= 3 else 403), managed.text
        assert (await client.delete(path, headers=member)).status_code in (403, 404)
        if level == 1:
            forked = await client.post(path + "/fork", headers=member, json={})
            assert forked.status_code == 201, forked.text
            assert forked.json()["permission_level"] == 4
        revoked = await client.delete(path + "/permissions/" + principal, headers=owner)
        assert revoked.status_code == 204, revoked.text
        assert (await client.get(path, headers=member)).status_code == 404
        listed = await client.get("/v1/sessions?visibility=shared", headers=member)
        assert all(item["id"] != session_id for item in listed.json()["data"])


@pytest.mark.asyncio
async def test_legacy_individual_group_key_is_listed_as_user_and_cannot_be_converted(
    machine_app, db_uri, oidc, signing_key
):
    from tests.server.routes.test_session_updates_ws import _seed_session

    store = SqlAlchemyPermissionStore(db_uri)
    session_id = _seed_session(
        (SqlAlchemyConversationStore(db_uri), SqlAlchemyAgentStore(db_uri), store),
        owner="owner@example.test",
        title="Legacy individual share",
    )
    principal = group_principal("/engineering")
    store.grant(principal, session_id, 2)
    owner = {
        "Authorization": "Bearer "
        + mint_session_token("owner@example.test", oidc.cookie_secret, 300, "oidc")
    }
    member = {"Authorization": "Bearer " + human_token(signing_key, groups=["/engineering"])}
    path = f"/v1/sessions/{session_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=machine_app), base_url="https://app.example.test"
    ) as client:
        response = await client.get(path + "/permissions", headers=owner)
        assert response.status_code == 200, response.text
        legacy = next(
            grant for grant in response.json()["permissions"] if grant["user_id"] == principal
        )
        assert legacy["principal_type"] == "user"
        assert legacy["group_name"] is None
        assert (await client.get(path, headers=member)).status_code == 404
        response = await client.put(
            path + "/permissions",
            headers=owner,
            json={"principal_type": "group", "user_id": "/engineering", "level": 1},
        )
        assert response.status_code == 409, response.text
        assert store.get(principal, session_id).level == 2
        assert not store.get(principal, session_id).is_group
        assert (
            await client.delete(path + "/permissions/" + principal, headers=owner)
        ).status_code == 204
        response = await client.put(
            path + "/permissions",
            headers=owner,
            json={"principal_type": "group", "user_id": "/engineering", "level": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["principal_type"] == "group"
        assert (await client.get(path, headers=member)).status_code == 200
        # Reserved delegated subjects reject cleanly before ensure_user can throw.
        reserved = {"Authorization": "Bearer " + human_token(signing_key, email=principal)}
        assert (
            await client.get("/v1/sessions?visibility=all", headers=reserved)
        ).status_code == 401
