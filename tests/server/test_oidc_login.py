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
    broker.capture(OWNER, SUBJECT, tokens())
    auth = UnifiedAuthProvider("oidc", oidc_config=broker.config)
    auth.oidc_login_broker = broker
    grants = DeviceGrantStore(db_uri)
    refresh = issue_login_grant(grants, user_id=OWNER, cookie_secret=broker.config.cookie_secret)
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
