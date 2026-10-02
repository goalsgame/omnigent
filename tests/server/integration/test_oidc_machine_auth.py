"""OIDC machine session ownership, human sharing and live admin exclusion."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.oidc import mint_session_token
from omnigent.server.oidc_human_auth import OIDCHumanConfig, OIDCHumanVerifier
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle
from tests.server.test_oidc_human_auth import human_token
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

# Reuse RSA and issuer fixtures, without network access.
__all__ = ["machine_config", "oidc", "signing_key", "verifier"]


@pytest.fixture()
def machine_app(runtime_init, db_uri, tmp_path, oidc, verifier, machine_config) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        allowed_domains=["example.test"],
        auth_provider=UnifiedAuthProvider(
            "oidc",
            oidc_config=oidc,
            machine_verifier=verifier,
            human_verifier=OIDCHumanVerifier(
                OIDCHumanConfig(
                    "agent-api", "omnigent-access", frozenset({"connectors-exchange"})
                ),
                oidc,
                frozenset(machine_config.clients.values()),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_bot_owns_session_and_can_share_without_cross_tenant_access(
    machine_app, signing_key, oidc, db_uri
):
    machine = {"Authorization": "Bearer " + signed_token(signing_key)}
    human = {
        "Authorization": "Bearer "
        + mint_session_token("person@example.test", oidc.cookie_secret, 300, "oidc")
    }
    other = {
        "Authorization": "Bearer "
        + mint_session_token("other@example.test", oidc.cookie_secret, 300, "oidc")
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=machine_app), base_url="https://app.example.test"
    ) as client:

        async def create(headers, name):
            response = await client.post(
                "/v1/sessions",
                data={"metadata": "{}"},
                files={
                    "bundle": ("agent.tar.gz", build_agent_bundle(name=name), "application/gzip")
                },
                headers=headers,
            )
            assert response.status_code == 201, response.text
            return response.json()["session_id"]

        bot_session = await create(machine, "machine-agent")
        human_session = await create(human, "human-agent")
        store = SqlAlchemyPermissionStore(db_uri)
        grant = store.get(PRINCIPAL, bot_session)
        assert grant is not None and grant.level == LEVEL_OWNER
        assert (
            await client.get(f"/v1/sessions/{bot_session}", headers=machine)
        ).status_code == 200
        assert (
            await client.get(f"/v1/sessions/{human_session}", headers=machine)
        ).status_code == 404
        assert (await client.get(f"/v1/sessions/{bot_session}", headers=human)).status_code == 404
        shared = await client.put(
            f"/v1/sessions/{bot_session}/permissions",
            json={"user_id": "person@example.test", "level": LEVEL_EDIT},
            headers=machine,
        )
        assert shared.status_code == 200, shared.text
        assert (await client.get(f"/v1/sessions/{bot_session}", headers=human)).status_code == 200
        edited = await client.patch(
            f"/v1/sessions/{bot_session}",
            json={"title": "Human continued the ticket"},
            headers=human,
        )
        assert edited.status_code == 200, edited.text
        assert (await client.get(f"/v1/sessions/{bot_session}", headers=other)).status_code == 404
        assert (await client.get("/v1/me", headers=machine)).status_code == 401
        assert (await client.get("/v1/me", headers=human)).status_code == 200
        store.set_admin(PRINCIPAL, True)
        assert (
            await client.get(f"/v1/sessions/{human_session}", headers=machine)
        ).status_code == 401
        store.set_admin(PRINCIPAL, False)
        assert (
            await client.delete(f"/v1/sessions/{bot_session}", headers=machine)
        ).status_code == 200
        assert (
            await client.get(f"/v1/sessions/{human_session}", headers=human)
        ).status_code == 200


@pytest.mark.asyncio
async def test_delegated_human_and_browser_share_identity_and_permissions(
    machine_app, signing_key, oidc, db_uri
):
    delegated = {"Authorization": "Bearer " + human_token(signing_key)}
    browser = {
        "Authorization": "Bearer "
        + mint_session_token("person@example.test", oidc.cookie_secret, 300, "oidc")
    }
    other = {
        "Authorization": "Bearer "
        + human_token(signing_key, sub="other-human", email="other@example.test")
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=machine_app), base_url="https://app.example.test"
    ) as client:
        denied = {
            "Authorization": "Bearer " + human_token(signing_key, email="outsider@other.test")
        }
        assert (await client.get("/v1/agents", headers=denied)).status_code == 401
        created = await client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="delegated"),
                    "application/gzip",
                )
            },
            headers=delegated,
        )
        assert created.status_code == 201, created.text
        session = created.json()["session_id"]
        owner = await client.get(f"/v1/sessions/{session}/owner", headers=browser)
        assert owner.json() == {"owner": "person@example.test"}
        assert (await client.get(f"/v1/sessions/{session}", headers=other)).status_code == 404
        assert (
            await client.put(
                f"/v1/sessions/{session}/permissions",
                headers=delegated,
                json={"user_id": "other@example.test", "level": 2},
            )
        ).status_code == 200
        assert (
            await client.patch(
                f"/v1/sessions/{session}", headers=other, json={"title": "Shared human edit"}
            )
        ).status_code == 200
        assert (await client.delete(f"/v1/sessions/{session}", headers=other)).status_code == 403
        assert (
            await client.delete(
                f"/v1/sessions/{session}/permissions/other@example.test", headers=delegated
            )
        ).status_code == 204
        assert (await client.get(f"/v1/sessions/{session}", headers=other)).status_code == 404
        assert (await client.delete(f"/v1/sessions/{session}", headers=browser)).status_code == 200
