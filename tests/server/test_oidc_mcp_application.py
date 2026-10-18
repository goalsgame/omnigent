"""API session writes capture verified IdP owners through application middleware."""

from urllib.parse import parse_qs

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_credential_store import _FakeCipher
from tests.server.test_oidc_human_auth import human, human_token
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    SUBJECT,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

__all__ = ["human", "machine_config", "oidc", "signing_key", "verifier"]


@pytest.fixture(params=["oidc_mcp", "oidc_lore"])
def delegation_app(request, db_uri, tmp_path, monkeypatch, runtime_init, oidc, human, verifier):
    monkeypatch.setattr(
        "omnigent.stores.credential_store.build_secret_cipher", lambda: _FakeCipher()
    )
    monkeypatch.setenv("TEST_MCP_CLIENT_SECRET", "fixture-secret")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=HostStore(db_uri),
        auth_provider=UnifiedAuthProvider(
            "oidc", oidc_config=oidc, human_verifier=human, machine_verifier=verifier
        ),
        server_config={
            request.param: {
                "upstream_url": "https://tools.example.test",
                "audience": "tools-api",
                "scope": "openid tools-api",
                "machine_client_id": "machine-exchange",
                "machine_client_secret_env": "TEST_MCP_CLIENT_SECRET",
                **(
                    {
                        "repositories": {"lores://tools.example.test/game": {}},
                        "machine_principals": [PRINCIPAL],
                    }
                    if request.param == "oidc_lore"
                    else {}
                ),
            }
        },
    )
    app.state.test_identity_broker = getattr(
        app.state,
        "lore_identity_broker" if request.param == "oidc_lore" else "mcp_identity_broker",
    )
    yield app
    app.state.test_identity_broker.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("path", ["/v1/sessions", "/v1/sessions/missing/events"])
async def test_session_post_captures_idp_owner(
    delegation_app, signing_key, oidc, machine, unavailable, path
):
    owner = PRINCIPAL if machine else "person@example.test"
    subject = SUBJECT if machine else "human-subject"
    token = signed_token(signing_key) if machine else human_token(signing_key)
    exchange_client = "machine-exchange" if machine else oidc.client_id
    exchanges = []

    def exchange(request):
        form = parse_qs(request.content.decode())
        exchanges.append(form)
        assert form["subject_token"] == [token]
        assert form["client_id"] == [exchange_client]
        if unavailable:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": signed_token(
                    signing_key, sub=subject, azp=exchange_client, aud="tools-api"
                ),
                "refresh_token": "server-only-refresh",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    broker = delegation_app.state.test_identity_broker
    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(exchange))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delegation_app), base_url="https://app.example.test"
    ) as client:
        response = await client.post(path, headers={"Authorization": f"Bearer {token}"}, json={})
    assert len(exchanges) == 1
    grant = broker.store.get(owner, broker.provider, with_secret=True)
    if unavailable:
        assert response.status_code == 403
        assert response.json() == {
            "detail": ("Lore" if broker.provider == "oidc_lore" else "MCP")
            + " identity delegation unavailable; renew your IdP session"
        }
        assert response.headers["cache-control"] == "no-store"
        assert grant is None
    else:
        # The invalid create body / missing session reaches the endpoint after capture.
        assert response.status_code in (404, 422), response.text
        assert grant is not None
        assert grant.metadata == {"issuer": oidc.issuer, "subject": subject}
        assert grant.secret["refresh_token"] == "server-only-refresh"
        other_owner = "person@example.test" if machine else PRINCIPAL
        assert broker.store.get(other_owner, broker.provider) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("GET", "/v1/sessions"), ("POST", "/v1/sessions-other")])
async def test_non_session_writes_do_not_exchange(delegation_app, signing_key, method, path):
    exchanges = []

    def exchange(request):
        exchanges.append(request)
        return httpx.Response(400)

    broker = delegation_app.state.test_identity_broker
    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(exchange))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delegation_app), base_url="https://app.example.test"
    ) as client:
        await client.request(
            method, path, headers={"Authorization": f"Bearer {human_token(signing_key)}"}
        )
    assert exchanges == []
    assert broker.store.get("person@example.test", broker.provider) is None


def test_disabled_renewal_keeps_revocation_available(delegation_app):
    app = delegation_app
    assert app.state.oidc_login_broker is None
    revoker = app.state.oidc_login_revoker
    assert revoker is not None
    assert not hasattr(revoker, "state")
