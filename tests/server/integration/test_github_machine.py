"""Real application wiring from launch-token ownership to scoped GitHub tokens."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omnigent.connections.github import GithubConnectionStore
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.github_app import GitHubAppConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.github_app_fixtures import make_config
from tests.server.test_github_store import SecretBox
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    machine_config,
    oidc,
    signing_key,
    verifier,
)

__all__ = ["machine_config", "oidc", "signing_key", "verifier"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_user_store", [False, True])
async def test_app_vends_scoped_bot_token_and_rechecks_admin_exclusion(
    runtime_init,
    db_uri,
    tmp_path,
    oidc,
    verifier,
    monkeypatch,
    with_user_store,
):
    artifact = LocalArtifactStore(str(tmp_path / "artifacts"))
    hosts = HostStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(PRINCIPAL)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact,
        agent_cache=AgentCache(artifact_store=artifact, cache_dir=tmp_path / "cache"),
        host_store=hosts,
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier),
        github_config=replace(make_config(), app_id="123", private_key="test-key"),
        github_store=(
            GithubConnectionStore(db_uri, SecretBox("test-key")) if with_user_store else None
        ),
        server_config={
            "github_machine_auth": {
                PRINCIPAL: {"installation_id": 123, "repository_ids": [456], "access": "write"}
            }
        },
    )
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "token": "ghs_bot",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )

    monkeypatch.setattr(GitHubAppConfig, "mint_app_jwt", lambda self: "app-jwt")
    assert (app.state.github_client is not None) == with_user_store
    app.state.github_machine_broker._client._transport = httpx.MockTransport(handle)
    hosts.register_managed_host(
        host_id="00000000000000000000000000000001",
        name="bot",
        user_id=PRINCIPAL,
        token="launch-token",
        provider="agent_sandbox",
        sandbox_id="sandbox",
        token_expires_at=int(time.time()) + 300,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        path = "/v1/hosts/00000000000000000000000000000001/credentials/github"
        headers = {"X-Omnigent-Host-Token": "launch-token"}
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["token"] == "ghs_bot"
        assert response.json()["owner"] == PRINCIPAL
        assert calls == [
            {
                "repository_ids": [456],
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            }
        ]
        permissions.set_admin(PRINCIPAL, True)
        assert (await client.get(path, headers=headers)).json() == {
            "connected": False,
            "reason": "machine_not_authorized",
        }
        assert len(calls) == 1
        permissions.set_admin(PRINCIPAL, False)
        app.state.github_machine_broker._bindings.clear()
        assert (await client.get(path, headers=headers)).json() == {
            "connected": False,
            "reason": "machine_not_authorized",
        }
        assert len(calls) == 1
        hosts.revoke_launch_token("00000000000000000000000000000001")
        assert (await client.get(path, headers=headers)).status_code == 401
