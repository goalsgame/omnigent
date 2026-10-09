"""Machine session opt-in with real OIDC authentication and application wiring."""

from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

__all__ = ["machine_config", "oidc", "signing_key", "verifier"]


@pytest.mark.asyncio
async def test_machine_create_opt_in(
    runtime_init, db_uri, tmp_path, oidc, verifier, signing_key, monkeypatch
):
    artifact = LocalArtifactStore(str(tmp_path / "artifacts"))
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact,
        agent_cache=AgentCache(artifact_store=artifact, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier),
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://app.example.test",
                "modal": {"image": "example/host:latest"},
            }
        ),
        server_config={
            "google_cloud_machine_auth": {
                PRINCIPAL: {"service_account": "test-bot@example-project.iam.gserviceaccount.com"}
            }
        },
    )
    # Provisioning is external to the API contract; keep this test entirely local.
    from omnigent.server.routes.sessions import routes_core

    monkeypatch.setattr(routes_core, "_run_managed_launch", AsyncMock())
    headers = {"Authorization": "Bearer " + signed_token(signing_key)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://app.example.test",
        headers=headers,
    ) as client:
        info = (await client.get("/v1/info")).json()
        assert info["machine_google_cloud_enabled"] is True
        assert "google_cloud" not in info["enabled_connections"]
        agent = await create_test_agent(client)
        for enabled in (False, True):
            response = await client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent["id"],
                    "host_type": "managed",
                    "google_cloud_access": enabled,
                },
            )
            assert response.status_code == 201, response.text
            sid = response.json()["id"]
            status = await client.get(f"/v1/sessions/{sid}/google-cloud")
            assert status.status_code == 200, status.text
            assert status.json()["state"] == ("allowed" if enabled else "off")
        from omnigent.server.oidc import mint_session_token

        delegated = mint_session_token(
            PRINCIPAL, oidc.cookie_secret, 300, "oidc", credential_delegate=True
        )
        rejected = await client.post(
            f"/v1/sessions/{sid}/google-cloud",
            json={"enabled": False},
            headers={"Authorization": "Bearer " + delegated},
        )
        assert rejected.status_code == 403
        rejected = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "host_type": "managed", "google_cloud_access": True},
            headers={"Authorization": "Bearer " + delegated},
        )
        assert rejected.status_code == 403
        assert (await client.get(f"/v1/sessions/{sid}/google-cloud")).json()["state"] == "allowed"
        permissions.set_admin(PRINCIPAL, True)
        assert (await client.get(f"/v1/sessions/{sid}/google-cloud")).status_code == 401
