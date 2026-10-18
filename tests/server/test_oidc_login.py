"""Offline login deadlines, rotation, browser renewal and owner isolation."""

import dataclasses
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request

from omnigent.server.oidc_login import PROVIDER, OIDCLoginBroker
from omnigent.server.oidc_login_middleware import OIDCLoginMiddleware
from omnigent.server.oidc_mcp import DelegationUnavailable
from omnigent.stores.credential_store import CredentialStore
from tests.server.test_credential_store import _FakeCipher
from tests.server.test_oidc_machine_auth import oidc, signing_key

__all__ = ["oidc", "signing_key"]
OWNER, SUBJECT = "person@example.test", "human-subject"


@pytest.fixture
def login(db_uri, monkeypatch, oidc, signing_key):
    config = dataclasses.replace(oidc, scopes="openid email profile offline_access")
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid="fixture", alg="RS256", use="sig")
    monkeypatch.setattr(
        "omnigent.server.oidc_login._BoundedJWKClient.get_signing_key_from_jwt",
        lambda self, token: jwt.PyJWK.from_dict(jwk),
    )
    broker = OIDCLoginBroker(config, CredentialStore(db_uri, _FakeCipher()), 168)

    def token(kind="Bearer", subject=SUBJECT, groups=None):
        now = int(time.time())
        return jwt.encode(
            {
                "iss": config.issuer,
                "sub": subject,
                "azp": config.client_id,
                "aud": config.client_id,
                "email_verified": True,
                "iat": now,
                "exp": now + (900 if kind == "Bearer" else 604800),
                "typ": kind,
                "email": OWNER,
                "groups": ["engineers"] if groups is None else groups,
            },
            signing_key,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )

    def tokens():
        return {"access_token": token(), "refresh_token": token("Offline"), "expires_in": 900}

    calls = []

    def refresh(request):
        form = parse_qs(request.content.decode())
        calls.append(form)
        assert form["grant_type"] == ["refresh_token"]
        return httpx.Response(200, json=tokens())

    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(refresh))
    yield broker, token, tokens, calls
    broker.client.close()


def test_weekly_cap_and_shared_refresh(login):
    broker, _token, tokens, calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    assert 604790 < state["deadline"] - time.time() <= 604800
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    renewed = broker.state(OWNER)
    assert len(calls) == 1
    assert renewed["deadline"] == state["deadline"]
    assert broker.state(OWNER)["access_token"] == renewed["access_token"]
    assert len(calls) == 1
    broker.store.upsert(
        OWNER, PROVIDER, secret=renewed, metadata={**renewed, "deadline": time.time() - 1}
    )
    with pytest.raises(DelegationUnavailable):
        broker.state(OWNER)
    assert len(calls) == 1


@pytest.mark.parametrize("kind,subject", [("Refresh", SUBJECT), ("Offline", "other-person")])
def test_reject_online_or_wrong_owner_grant(login, kind, subject):
    broker, token, tokens, _calls = login
    value = tokens()
    value["refresh_token"] = token(kind, subject)
    with pytest.raises(DelegationUnavailable):
        broker.capture(OWNER, SUBJECT, value)
    assert broker.store.get(OWNER, PROVIDER) is None


def test_isolation_revocation_and_generation(login):
    broker, _token, tokens, _calls = login
    first = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, first)
    with pytest.raises(DelegationUnavailable):
        broker.state("someone-else@example.test")
    second = broker.capture(OWNER, SUBJECT, tokens())
    assert first["generation"] != second["generation"]
    with pytest.raises(DelegationUnavailable):
        broker.browser_identity(cookie)
    broker.store.delete(OWNER, PROVIDER)
    assert not broker.active(OWNER, second["generation"])


@pytest.mark.asyncio
async def test_browser_renews_short_token_and_fails_after_revocation(login):
    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)

    @app.get("/v1/probe")
    def probe(request: Request):
        return jwt.decode(
            request.cookies[broker.config.session_cookie_name],
            broker.config.cookie_secret,
            algorithms=["HS256"],
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.test"
    ) as client:
        client.cookies.set(broker.cookie_name, broker.browser_cookie(OWNER, state))
        response = await client.get("/v1/probe")
        assert response.status_code == 200
        claims = response.json()
        assert claims["sub"] == OWNER
        assert 0 < claims["exp"] - time.time() <= 900
        assert claims["group_authority"]["groups"] == ["engineers"]
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie
        broker.store.delete(OWNER, PROVIDER)
        response = await client.get("/v1/probe")
        assert response.status_code == 401
        assert response.json()["code"] == "identity_login_required"


@pytest.mark.asyncio
async def test_native_refresh_uses_login_deadline_and_current_groups(login, db_uri):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(
        grants,
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    app = FastAPI()
    app.include_router(create_oauth_token_router(auth, grants))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.test"
    ) as client:
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert response.status_code == 200
        claims = jwt.decode(
            response.json()["access_token"], broker.config.cookie_secret, algorithms=["HS256"]
        )
        assert claims["group_authority"]["groups"] == ["engineers"]
        assert 0 < response.json()["expires_in"] <= 900
        assert broker.active(OWNER, claims["oidc_grant_id"])
        broker.store.delete(OWNER, PROVIDER)
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        broker.capture(OWNER, SUBJECT, tokens())
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"


def test_concurrent_refresh_rotates_the_single_shared_grant(login):
    from concurrent.futures import ThreadPoolExecutor

    broker, _token, tokens, calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: broker.state(OWNER), range(2)))
    assert len(calls) == 1
    assert states[0] == states[1]


def test_renewal_cookie_is_not_an_access_token(login):
    from starlette.requests import Request

    from omnigent.server.auth import UnifiedAuthProvider

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, state)
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [(b"authorization", ("Bearer " + cookie).encode())],
        }
    )
    assert auth.get_user_id(request) is None
    access = broker.session_token(OWNER, state)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [(b"authorization", ("Bearer " + access).encode())],
        }
    )
    assert auth.get_user_id(request) == OWNER
    broker.store.delete(OWNER, PROVIDER)
    assert auth.get_user_id(request) is None


@pytest.mark.asyncio
async def test_expired_chat_link_redirects_back_to_login(login):
    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, state)
    broker.store.delete(OWNER, PROVIDER)
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.test"
    ) as client:
        client.cookies.set(broker.cookie_name, cookie)
        response = await client.get("/c/fixture")
        assert response.status_code == 302
        assert response.headers["location"] == "/auth/login?return_to=%2Fc%2Ffixture"
        assert "no-store" in response.headers["cache-control"]


def test_refresh_removes_group_authority(login):
    broker, token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    broker.client.close()
    broker.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "access_token": token(groups=[]),
                    "refresh_token": token("Offline"),
                    "expires_in": 900,
                },
            )
        )
    )
    state = broker.state(OWNER)
    assert broker.group_authority(state)["groups"] == []


def test_capture_rejects_replaced_generation(login, monkeypatch):
    broker, _token, tokens, _calls = login
    original = broker.store.upsert

    def replacing(owner, provider, **kwargs):
        original(owner, provider, **kwargs)
        original(
            owner,
            provider,
            secret=kwargs["secret"],
            metadata={**kwargs["metadata"], "generation": "replacement"},
        )

    monkeypatch.setattr(broker.store, "upsert", replacing)
    with pytest.raises(DelegationUnavailable):
        broker.capture(OWNER, SUBJECT, tokens())


def test_logout_cannot_delete_concurrently_replaced_login(login, monkeypatch):
    broker, _token, tokens, _calls = login
    first = broker.capture(OWNER, SUBJECT, tokens())
    original = broker.store.delete
    replacement = {}

    def replacing(owner, provider, **kwargs):
        replacement.update(broker.capture(OWNER, SUBJECT, tokens()))
        return original(owner, provider, **kwargs)

    monkeypatch.setattr(broker.store, "delete", replacing)
    assert not broker.revoke(OWNER, first["generation"])
    assert broker.active(OWNER, replacement["generation"])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/settings", "/c/fixture"])
async def test_revoked_browser_document_redirects_but_api_stays_json(login, path):
    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, state)
    broker.store.delete(OWNER, PROVIDER)
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.test"
    ) as client:
        client.cookies.set(broker.cookie_name, cookie)
        response = await client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 302
        client.cookies.set(broker.cookie_name, cookie)
        response = await client.get("/v1/me", headers={"accept": "text/html"})
        assert response.status_code == 401
        assert response.json()["code"] == "identity_login_required"


@pytest.mark.asyncio
async def test_silent_browser_renewal_does_not_satisfy_device_reauthentication(
    login, db_uri, monkeypatch
):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.device_auth import create_device_auth_router

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens(), auth_time=int(time.time()) - 3600)
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    app = FastAPI()
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", broker.config.base_url)
    app.include_router(create_device_auth_router(auth, DeviceGrantStore(db_uri)))
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        request = await client.post("/oauth/device/authorize", json={"client_id": "fixture"})
        code = request.json()["user_code"]
        client.cookies.set(broker.cookie_name, broker.browser_cookie(OWNER, state))
        response = await client.get("/oauth/device", params={"user_code": code})
        assert response.status_code == 302
        assert "reauth=1" in response.headers["location"]
        response = await client.post(
            "/oauth/device/approve",
            data={"user_code": code},
            headers={"origin": broker.config.base_url},
        )
        assert response.status_code == 200
        assert "session is too old" in response.text


@pytest.fixture
def offline_auth_app(login, db_uri, tmp_path, monkeypatch):
    from omnigent.server.admin_list import AdminList
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.auth import create_auth_router

    broker, token, tokens, _calls = login
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    app = FastAPI()
    app.state.oidc_login_broker = broker
    app.include_router(
        create_auth_router(
            auth, None, AdminList(tmp_path / "admins"), device_grant_store=DeviceGrantStore(db_uri)
        ),
        prefix="/auth",
    )
    original = httpx.AsyncClient.post

    async def idp(client, url, **kwargs):
        if str(url) == broker.config.token_endpoint:
            return httpx.Response(200, json={**tokens(), "id_token": token("ID")})
        return await original(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", idp)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, value: broker.jwks.get_signing_key_from_jwt(value),
    )
    return app, broker, tokens


@pytest.mark.asyncio
async def test_native_code_cannot_redeem_replacement_generation(offline_auth_app):
    from urllib.parse import parse_qs, urlsplit

    from omnigent.server.oidc import derive_code_challenge

    app, broker, tokens = offline_auth_app
    verifier = "x" * 43
    redirect = "http://127.0.0.1:1234/callback"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        login_response = await client.get(
            "/auth/login",
            params={
                "native_redirect_uri": redirect,
                "native_state": "fixture-state",
                "code_challenge": derive_code_challenge(verifier),
                "code_challenge_method": "S256",
            },
        )
        state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
        callback = await client.get("/auth/callback", params={"state": state, "code": "fixture"})
        assert callback.status_code == 302, callback.text
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        broker.capture(OWNER, SUBJECT, tokens())
        response = await client.post(
            "/auth/native-token",
            data={"code": code, "code_verifier": verifier, "redirect_uri": redirect},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_expired_cli_access_ticket_returns_controlled_expiry(offline_auth_app, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    app, broker, _tokens = offline_auth_app
    expired = jwt.encode(
        {"sub": OWNER, "iat": int(time.time()) - 120, "exp": int(time.time()) - 1},
        broker.config.cookie_secret,
        algorithm="HS256",
    )
    monkeypatch.setattr(broker, "session_token", lambda owner, state: expired)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        ticket = (await client.post("/auth/cli-login")).json()["ticket"]
        login_response = await client.get("/auth/login", params={"ticket": ticket})
        state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
        callback = await client.get("/auth/callback", params={"state": state, "code": "fixture"})
        assert callback.status_code == 200
        response = await client.get("/auth/cli-poll", params={"ticket": ticket})
        assert response.status_code == 410
        assert response.json() == {"error": "Ticket token expired"}
        assert "refresh_token" not in response.json()


@pytest.mark.asyncio
async def test_stale_device_consent_cookie_redirect_preserves_user_code(login):
    from urllib.parse import parse_qs, urlsplit

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, state)
    broker.revoke(OWNER, state["generation"])
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        client.cookies.set(broker.cookie_name, cookie)
        response = await client.get("/oauth/device", params={"user_code": "FIXTURE-CODE"})
        assert response.status_code == 302
        assert parse_qs(urlsplit(response.headers["location"]).query)["return_to"] == [
            "/oauth/device?user_code=FIXTURE-CODE"
        ]
        client.cookies.set(broker.cookie_name, cookie)
        response = await client.post("/oauth/token", data={"grant_type": "refresh_token"})
        assert response.status_code == 401
        assert response.json()["code"] == "identity_login_required"


@pytest.mark.parametrize("revoked", [False, True])
def test_websocket_can_authenticate_with_only_renewal_cookie(login, revoked):
    from fastapi import WebSocket
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from omnigent.server.auth import UnifiedAuthProvider

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    cookie = broker.browser_cookie(OWNER, state)
    if revoked:
        broker.revoke(OWNER, state["generation"])
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)

    @app.websocket("/v1/socket")
    async def socket(websocket: WebSocket):
        user = auth.get_user_id(websocket)
        assert user == OWNER
        await websocket.accept()
        await websocket.send_json({"user": user})
        await websocket.close()

    with TestClient(app) as client:
        client.cookies.set(broker.cookie_name, cookie)
        if revoked:
            with (
                pytest.raises(WebSocketDisconnect) as failure,
                client.websocket_connect("/v1/socket"),
            ):
                pass
            assert failure.value.code == 4401
        else:
            with client.websocket_connect("/v1/socket") as connection:
                assert connection.receive_json() == {"user": OWNER}


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_native_logout_revokes_only_original_login(login, db_uri, superseded):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    access = broker.session_token(OWNER, state)
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(
        grants,
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    current = broker.capture(OWNER, SUBJECT, tokens()) if superseded else state
    app = FastAPI()
    app.include_router(create_oauth_token_router(auth, grants))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        response = await client.post("/oauth/revoke", data={"refresh_token": refresh})
        assert response.status_code == 200
        assert not broker.active(OWNER, state["generation"])
        assert broker.active(OWNER, current["generation"]) is superseded
        assert (
            grants.get_by_refresh_hash(hash_secret(refresh, broker.config.cookie_secret)) is None
        )
        from starlette.requests import Request as StarletteRequest

        request = StarletteRequest(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/me",
                "headers": [(b"authorization", f"Bearer {access}".encode())],
            }
        )
        assert auth.get_user_id(request) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["active", "revoked", "superseded"])
async def test_disabled_broker_cannot_unbind_native_refresh(login, db_uri, authority):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(
        grants,
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    if authority == "revoked":
        broker.revoke(OWNER, state["generation"])
    elif authority == "superseded":
        broker.capture(OWNER, SUBJECT, tokens())
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    app = FastAPI()
    app.include_router(create_oauth_token_router(auth, grants))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert "access_token" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_logout_while_renewal_disabled_stays_revoked_after_reenable(
    offline_auth_app, db_uri, native
):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.oidc_login import OIDCLoginRevoker
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    app, broker, tokens = offline_auth_app
    state = broker.capture(OWNER, SUBJECT, tokens())
    cookie = broker.browser_cookie(OWNER, state)
    revoker = OIDCLoginRevoker(
        dataclasses.replace(broker.config, scopes="openid email profile"), broker.store
    )
    app.state.oidc_login_broker = None
    app.state.oidc_login_revoker = revoker
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_revoker = revoker
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(
        grants,
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    app.include_router(create_oauth_token_router(auth, grants))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        client.cookies.set(broker.cookie_name, cookie)
        if native:
            response = await client.post("/oauth/revoke", data={"refresh_token": refresh})
        else:
            response = await client.get("/auth/logout")
            assert broker.cookie_name in response.headers["set-cookie"]
            assert "Max-Age=0" in response.headers["set-cookie"]
        assert response.status_code in (200, 302)
        from omnigent.server.oidc_mcp import MCPDelegationBroker, MCPDelegationConfig

        for provider in ("oidc_mcp", "oidc_lore"):
            broker.store.upsert(
                OWNER,
                provider,
                secret={"access_token": "cached", "expires_at": time.time() + 300},
                metadata={
                    "issuer": broker.config.issuer,
                    "subject": SUBJECT,
                    "generation": state["generation"],
                },
            )
            service = MCPDelegationBroker(
                MCPDelegationConfig(
                    "https://tools.example.test", "tools", "openid tools", "machine", "secret"
                ),
                broker.config,
                broker.store,
                auth,
                provider=provider,
            )
            try:
                with pytest.raises(DelegationUnavailable, match="Sign in again"):
                    service.credential(OWNER)
            finally:
                service.client.close()
        auth.oidc_login_broker = broker
        app.state.oidc_login_broker = broker
        assert not broker.active(OWNER, state["generation"])
        with pytest.raises(DelegationUnavailable):
            broker.browser_identity(cookie)
        response = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failure", [503, 429, "timeout"])
async def test_temporary_provider_failure_preserves_browser_and_native_renewal(
    login, db_uri, native, failure
):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    calls = []

    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("provider unavailable", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json=tokens())

    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(provider))
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    refresh = issue_login_grant(
        DeviceGrantStore(db_uri),
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    app.include_router(create_oauth_token_router(auth, DeviceGrantStore(db_uri)))

    @app.get("/v1/probe")
    def probe():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        if not native:
            client.cookies.set(broker.cookie_name, broker.browser_cookie(OWNER, state))

        async def renew():
            if native:
                return await client.post(
                    "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
                )
            return await client.get("/v1/probe")

        response = await renew()
        assert response.status_code == 503
        assert response.json()["error"] == "temporarily_unavailable"
        assert "set-cookie" not in response.headers
        assert broker.active(OWNER, state["generation"])
        assert (await renew()).status_code == 200
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("key_failure", ["network", "rate-limit"])
async def test_signing_key_outage_preserves_rotated_response_until_verified(
    login, db_uri, monkeypatch, signing_key, native, key_failure
):
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.oidc_machine_auth import _BoundedJWKClient
    from omnigent.server.routes.device_auth import create_oauth_token_router, issue_login_grant

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    rotated = tokens()
    claims = jwt.decode(rotated["refresh_token"], options={"verify_signature": False})
    claims["jti"] = "rotated-once"
    rotated["refresh_token"] = jwt.encode(
        claims, signing_key, algorithm="RS256", headers={"kid": "fixture"}
    )
    unavailable = [False]
    original_key = broker.jwks.get_signing_key_from_jwt
    limited = _BoundedJWKClient(broker.config.jwks_uri)
    limited._next_fetch = time.monotonic() + 30

    def key(token):
        if unavailable[0]:
            if key_failure == "rate-limit":
                limited.fetch_data()
            raise jwt.PyJWKClientConnectionError("temporary fetch failure")
        return original_key(token)

    monkeypatch.setattr(broker.jwks, "get_signing_key_from_jwt", key)
    requests = []

    def provider(request):
        requests.append(request)
        assert len(requests) == 1, "must retry verification without reusing rotated refresh token"
        unavailable[0] = True
        return httpx.Response(200, json=rotated)

    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(provider))
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(
        grants,
        user_id=OWNER,
        cookie_secret=broker.config.cookie_secret,
        oidc_login_generation=state["generation"],
    )
    app = FastAPI()
    app.add_middleware(OIDCLoginMiddleware, broker=broker)
    app.include_router(create_oauth_token_router(auth, grants))

    @app.get("/v1/probe")
    def probe():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=broker.config.base_url
    ) as client:
        if not native:
            client.cookies.set(broker.cookie_name, broker.browser_cookie(OWNER, state))

        async def renew():
            if native:
                return await client.post(
                    "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
                )
            return await client.get("/v1/probe")

        for _ in range(2):
            response = await renew()
            assert response.status_code == 503
            assert "set-cookie" not in response.headers
            assert "access_token" not in response.json()
        pending = broker.store.get(OWNER, PROVIDER, with_secret=True).secret
        assert pending["refresh_token"] == state["refresh_token"]
        assert pending["pending_tokens"]["refresh_token"] == rotated["refresh_token"]
        unavailable[0] = False
        assert (await renew()).status_code == 200
        verified = broker.state(OWNER)
        assert "pending_tokens" not in verified
        assert verified["refresh_token"] == rotated["refresh_token"]
        assert verified["generation"] == state["generation"]
        assert len(requests) == 1


def test_pending_rotated_response_never_accepts_wrong_identity(login, monkeypatch):
    from omnigent.server.oidc_login import DelegationTemporarilyUnavailable

    broker, token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    original = broker.jwks.get_signing_key_from_jwt
    unavailable = [True]

    def key(value):
        if unavailable[0]:
            raise jwt.PyJWKClientConnectionError("temporary failure")
        return original(value)

    monkeypatch.setattr(broker.jwks, "get_signing_key_from_jwt", key)
    bad = {**tokens(), "access_token": token(subject="different-subject")}
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    broker.client.close()
    broker.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bad))
    )
    with pytest.raises(DelegationTemporarilyUnavailable):
        broker.state(OWNER)
    unavailable[0] = False
    with pytest.raises(DelegationUnavailable):
        broker.state(OWNER)


@pytest.mark.parametrize("remaining", [-1, 5, 10, 30])
def test_expired_pending_access_renews_with_verified_rotated_refresh(
    login, signing_key, remaining
):
    broker, _token, tokens, calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    pending = tokens()
    access = jwt.decode(pending["access_token"], options={"verify_signature": False})
    access["exp"] = int(time.time()) + remaining
    pending["access_token"] = jwt.encode(
        access, signing_key, algorithm="RS256", headers={"kid": "fixture"}
    )
    refresh = jwt.decode(pending["refresh_token"], options={"verify_signature": False})
    refresh["jti"] = "already-rotated"
    pending["refresh_token"] = jwt.encode(
        refresh, signing_key, algorithm="RS256", headers={"kid": "fixture"}
    )
    broker.store.update_secret(
        OWNER, PROVIDER, secret={**state, "expires_at": 0, "pending_tokens": pending}
    )
    result = broker.state(OWNER)
    assert "pending_tokens" not in result
    assert calls[0]["refresh_token"] == [pending["refresh_token"]]
    assert result["generation"] == state["generation"]


def test_pending_renewal_without_refresh_field_preserves_effective_token(
    login, signing_key, monkeypatch
):
    from omnigent.server.oidc_login import DelegationTemporarilyUnavailable

    broker, _token, tokens, _calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    pending = tokens()
    access = jwt.decode(pending["access_token"], options={"verify_signature": False})
    access["exp"] = int(time.time()) - 1
    pending["access_token"] = jwt.encode(
        access, signing_key, algorithm="RS256", headers={"kid": "fixture"}
    )
    refresh = jwt.decode(pending["refresh_token"], options={"verify_signature": False})
    refresh["jti"] = "rotated-refresh"
    pending["refresh_token"] = jwt.encode(
        refresh, signing_key, algorithm="RS256", headers={"kid": "fixture"}
    )
    broker.store.update_secret(
        OWNER, PROVIDER, secret={**state, "expires_at": 0, "pending_tokens": pending}
    )
    unavailable = [False]
    original = broker.jwks.get_signing_key_from_jwt

    def key(value):
        if unavailable[0]:
            raise jwt.PyJWKClientConnectionError("key lookup failed")
        return original(value)

    monkeypatch.setattr(broker.jwks, "get_signing_key_from_jwt", key)
    requests = []
    response_tokens = tokens()
    response_tokens.pop("refresh_token")

    def provider(request):
        requests.append(parse_qs(request.content.decode()))
        assert requests[-1]["refresh_token"] == [pending["refresh_token"]]
        unavailable[0] = True
        return httpx.Response(200, json=response_tokens)

    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(provider))
    with pytest.raises(DelegationTemporarilyUnavailable):
        broker.state(OWNER)
    stored = broker.store.get(OWNER, PROVIDER, with_secret=True).secret
    assert stored["pending_tokens"]["refresh_token"] == pending["refresh_token"]
    unavailable[0] = False
    recovered = broker.state(OWNER)
    assert recovered["refresh_token"] == pending["refresh_token"]
    assert "pending_tokens" not in recovered
    assert len(requests) == 1
    broker.store.update_secret(OWNER, PROVIDER, secret={**recovered, "expires_at": 0})
    with pytest.raises(DelegationTemporarilyUnavailable):
        broker.state(OWNER)
    unavailable[0] = False
    assert broker.state(OWNER)["refresh_token"] == pending["refresh_token"]
    assert len(requests) == 2


def test_unknown_rotated_key_during_populated_cache_cooldown_is_retryable(
    login, signing_key, monkeypatch
):
    from omnigent.server.oidc_login import DelegationTemporarilyUnavailable

    broker, _token, tokens, calls = login
    state = broker.capture(OWNER, SUBJECT, tokens())
    broker.store.update_secret(OWNER, PROVIDER, secret={**state, "expires_at": 0})
    rotated = tokens()
    for field in ("access_token", "refresh_token"):
        claims = jwt.decode(rotated[field], options={"verify_signature": False})
        rotated[field] = jwt.encode(
            claims, signing_key, algorithm="RS256", headers={"kid": "new-key"}
        )
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid="fixture", alg="RS256", use="sig")
    broker.jwks.jwk_set_cache.put({"keys": [jwk]})
    broker.jwks._next_fetch = time.monotonic() + 30
    monkeypatch.setattr(
        broker.jwks,
        "get_signing_key_from_jwt",
        jwt.PyJWKClient.get_signing_key_from_jwt.__get__(broker.jwks),
    )
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json=rotated)

    broker.client.close()
    broker.client = httpx.Client(transport=httpx.MockTransport(provider))
    with pytest.raises(DelegationTemporarilyUnavailable):
        broker.state(OWNER)
    assert broker.store.get(OWNER, PROVIDER, with_secret=True).secret["pending_tokens"] == rotated
    broker.jwks.jwk_set_cache.put({"keys": [{**jwk, "kid": "new-key"}]})
    recovered = broker.state(OWNER)
    assert recovered["refresh_token"] == rotated["refresh_token"]
    assert "pending_tokens" not in recovered
    assert len(requests) == 1
    assert not calls
