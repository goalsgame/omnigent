"""API session writes capture verified IdP owners through application middleware."""

import dataclasses
import time
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


def _goproxy_app(db_uri, tmp_path, monkeypatch, oidc, human, verifier, server_config):
    monkeypatch.setattr(
        "omnigent.stores.credential_store.build_secret_cipher", lambda: _FakeCipher()
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
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
        server_config=server_config,
    )


def test_goproxy_is_off_without_configuration(
    db_uri, tmp_path, monkeypatch, runtime_init, oidc, human, verifier
):
    app = _goproxy_app(db_uri, tmp_path, monkeypatch, oidc, human, verifier, {})
    assert app.state.goproxy_identity_broker is None


def test_goproxy_requires_offline_delegation(
    db_uri, tmp_path, monkeypatch, runtime_init, oidc, human, verifier
):
    monkeypatch.delenv("OMNIGENT_OIDC_DELEGATION_TTL_HOURS", raising=False)
    config = {
        "oidc_goproxy": {
            "url": "https://go.example.test",
            "audience": "goproxy",
            "scope": "openid goproxy",
        }
    }
    with pytest.raises(RuntimeError, match="oidc_goproxy requires"):
        _goproxy_app(db_uri, tmp_path, monkeypatch, oidc, human, verifier, config)


def test_goproxy_broker_is_wired_to_the_login_broker(
    db_uri, tmp_path, monkeypatch, runtime_init, oidc, human, verifier
):
    monkeypatch.setenv("OMNIGENT_OIDC_DELEGATION_TTL_HOURS", "1")
    oidc = dataclasses.replace(oidc, scopes="openid offline_access")
    config = {
        "oidc_goproxy": {
            "url": "https://go.example.test",
            "audience": "goproxy",
            "scope": "openid goproxy",
        }
    }
    app = _goproxy_app(db_uri, tmp_path, monkeypatch, oidc, human, verifier, config)
    broker = app.state.goproxy_identity_broker
    assert broker.provider == "oidc_goproxy"
    assert broker.login_broker is app.state.oidc_login_broker is not None
    assert app.state.lore_identity_broker is None
    broker.client.close()


@pytest.fixture(params=["oidc_mcp", "oidc_lore"])
def login_app(request, db_uri, tmp_path, monkeypatch, runtime_init, oidc, human, verifier):
    monkeypatch.setenv("OMNIGENT_OIDC_DELEGATION_TTL_HOURS", "1")
    monkeypatch.setenv("TEST_MCP_CLIENT_SECRET", "fixture-secret")
    oidc = dataclasses.replace(oidc, scopes="openid email offline_access")
    server_config = {
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
        },
        "oidc_goproxy": {
            "url": "https://go.example.test",
            "audience": "tools-api",
            "scope": "openid tools-api",
        },
    }
    app = _goproxy_app(db_uri, tmp_path, monkeypatch, oidc, human, verifier, server_config)
    app.state.test_identity_broker = getattr(
        app.state,
        "lore_identity_broker" if request.param == "oidc_lore" else "mcp_identity_broker",
    )
    yield app
    app.state.test_identity_broker.client.close()
    app.state.goproxy_identity_broker.client.close()
    app.state.oidc_login_broker.client.close()


class _Idp:
    """Token endpoint stand-in recording each request against the login or delegation client."""

    def __init__(self, signing_key, oidc, *, login=None, subject="human-subject"):
        self.signing_key, self.oidc, self.subject = signing_key, oidc, subject
        self.login = login or self._grant
        self.requests = []

    def _grant(self, form):
        access = signed_token(
            self.signing_key,
            sub=self.subject,
            azp=self.oidc.client_id,
            email="person@example.test",
            aud="web",
        )
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": "offline-refresh",
                "expires_in": 300,
                "refresh_expires_in": 0,
                "scope": "openid email offline_access",
                "token_type": "Bearer",
            },
        )

    def handler(self, request):
        form = parse_qs(request.content.decode())
        self.requests.append(form)
        if form["client_id"] == [self.oidc.client_id] and form["grant_type"] != [
            "urn:ietf:params:oauth:grant-type:token-exchange"
        ]:
            return self._grant(form)
        if form.get("requested_token_type") == ["urn:ietf:params:oauth:token-type:refresh_token"]:
            return self.login(form)
        return httpx.Response(
            200,
            json={
                "access_token": signed_token(
                    self.signing_key,
                    sub=self.subject,
                    azp=self.oidc.client_id,
                    aud="tools-api",
                ),
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    def attach(self, app):
        for broker in (
            app.state.test_identity_broker,
            app.state.goproxy_identity_broker,
            app.state.oidc_login_broker,
        ):
            broker.client.close()
            broker.client = httpx.Client(transport=httpx.MockTransport(self.handler))


async def _post_session(app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.example.test"
    ) as client:
        return await client.post(
            "/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={}
        )


@pytest.mark.asyncio
async def test_bearer_without_login_state_captures_offline_grant(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc)
    idp.attach(login_app)
    token = human_token(signing_key)
    response = await _post_session(login_app, token)
    assert response.status_code in (404, 422), response.text
    exchange = next(
        form
        for form in idp.requests
        if form.get("requested_token_type") == ["urn:ietf:params:oauth:token-type:refresh_token"]
    )
    assert exchange["subject_token"] == [token]
    assert exchange["client_id"] == [oidc.client_id]
    assert "offline_access" in exchange["scope"][0].split()
    login = login_app.state.oidc_login_broker
    state = login.state("person@example.test")
    assert state["subject"] == "human-subject"
    assert state["refresh_token"] == "offline-refresh"
    assert login_app.state.test_identity_broker.credential("person@example.test")
    goproxy = login_app.state.goproxy_identity_broker
    assert goproxy.credential("person@example.test")
    assert goproxy.store.get("person@example.test", "oidc_goproxy") is not None


@pytest.mark.asyncio
async def test_existing_login_state_is_reused_without_a_new_exchange(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc)
    idp.attach(login_app)
    token = human_token(signing_key)
    await _post_session(login_app, token)
    exchanges = [f for f in idp.requests if "requested_token_type" in f]
    await _post_session(login_app, token)
    assert [f for f in idp.requests if "requested_token_type" in f] == exchanges


@pytest.mark.asyncio
async def test_subject_mismatch_never_overwrites_an_existing_grant(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc)
    idp.attach(login_app)
    await _post_session(login_app, human_token(signing_key))
    login = login_app.state.oidc_login_broker
    before = login.state("person@example.test")["generation"]
    idp.subject = "other-subject"
    response = await _post_session(login_app, human_token(signing_key, sub="other-subject"))
    assert response.status_code == 403
    assert "re-login" not in response.json()["detail"]
    assert login.state("person@example.test")["generation"] == before
    assert login.state("person@example.test")["subject"] == "human-subject"


@pytest.mark.asyncio
async def test_subject_mismatch_against_an_expired_grant_is_refused(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc)
    idp.attach(login_app)
    await _post_session(login_app, human_token(signing_key))
    login = login_app.state.oidc_login_broker
    current = login.store.get("person@example.test", "oidc_login", account_id="current")
    login.revoke("person@example.test", current.metadata["generation"])
    exchanges = [f for f in idp.requests if "requested_token_type" in f]
    idp.subject = "other-subject"
    response = await _post_session(login_app, human_token(signing_key, sub="other-subject"))
    assert response.status_code == 403
    assert [f for f in idp.requests if "requested_token_type" in f] == exchanges


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(400, json={"error": "invalid_scope"}),
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(
            200,
            json={
                "access_token": "unused",
                "refresh_token": "r",
                "expires_in": 300,
                "scope": "openid email",
            },
        ),
        httpx.Response(200, json={"access_token": "unused", "expires_in": 300}),
    ],
)
async def test_non_renewable_bearer_returns_distinct_403(login_app, signing_key, oidc, reply):
    idp = _Idp(signing_key, oidc, login=lambda form: reply)
    idp.attach(login_app)
    response = await _post_session(login_app, human_token(signing_key))
    assert response.status_code == 403
    assert response.json()["detail"].endswith("requires re-login with offline_access")
    assert response.headers["cache-control"] == "no-store"
    assert login_app.state.oidc_login_broker.known_subject("person@example.test") is None


@pytest.mark.asyncio
async def test_exchange_outage_returns_503(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc, login=lambda form: httpx.Response(503))
    idp.attach(login_app)
    response = await _post_session(login_app, human_token(signing_key))
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    assert response.headers["retry-after"] == "5"


@pytest.mark.asyncio
async def test_other_exchange_rejection_keeps_generic_403(login_app, signing_key, oidc):
    idp = _Idp(
        signing_key, oidc, login=lambda form: httpx.Response(403, json={"error": "access_denied"})
    )
    idp.attach(login_app)
    response = await _post_session(login_app, human_token(signing_key))
    assert response.status_code == 403
    assert response.json()["detail"].endswith("renew your IdP session")


@pytest.mark.asyncio
async def test_machine_owner_ignores_the_login_broker(login_app, signing_key, oidc):
    idp = _Idp(signing_key, oidc, subject=SUBJECT)

    def machine_exchange(request):
        form = parse_qs(request.content.decode())
        idp.requests.append(form)
        assert form["client_id"] == ["machine-exchange"]
        return httpx.Response(
            200,
            json={
                "access_token": signed_token(
                    signing_key, sub=SUBJECT, azp="machine-exchange", aud="tools-api"
                ),
                "refresh_token": "machine-refresh",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    broker = login_app.state.test_identity_broker
    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(machine_exchange))
    response = await _post_session(login_app, signed_token(signing_key))
    assert response.status_code in (404, 422), response.text
    grant = broker.store.get(PRINCIPAL, broker.provider, with_secret=True)
    assert grant.secret["refresh_token"] == "machine-refresh"
    assert grant.metadata == {"issuer": oidc.issuer, "subject": SUBJECT}
    assert login_app.state.oidc_login_broker.known_subject(PRINCIPAL) is None


@pytest.mark.asyncio
async def test_other_subject_refused_against_legacy_grant_whose_refresh_is_rejected(
    login_app, signing_key, oidc
):
    owner = "person@example.test"
    login = login_app.state.oidc_login_broker
    login.store.upsert(
        owner,
        "oidc_login",
        account_id="",
        secret={
            "access_token": "stale",
            "refresh_token": "legacy-refresh",
            "expires_at": 0,
        },
        metadata={
            "issuer": oidc.issuer,
            "subject": "human-subject",
            "generation": "legacy",
            "deadline": time.time() + 3600,
        },
    )
    idp = _Idp(signing_key, oidc, subject="other-subject")

    def reject_refresh(request):
        form = parse_qs(request.content.decode())
        idp.requests.append(form)
        if form["grant_type"] == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return idp.handler(request)

    for broker in (login_app.state.test_identity_broker, login):
        broker.client.close()
        broker.client = httpx.Client(transport=httpx.MockTransport(reject_refresh))
    response = await _post_session(login_app, human_token(signing_key, sub="other-subject"))
    assert response.status_code == 403
    assert not [f for f in idp.requests if "requested_token_type" in f]
    assert login.known_subject(owner) == "human-subject"
