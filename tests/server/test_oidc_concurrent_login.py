"""Independent pending browser logins retain their own state and PKCE verifier."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from tests.server.test_oidc_callback import _TEST_SECRET, _IdpKeys, callback_client

__all__ = ["callback_client"]


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_overlapping_logins_keep_their_verifier_and_destination(
    callback_client: tuple[TestClient, _IdpKeys], monkeypatch: pytest.MonkeyPatch, order
) -> None:
    client, keys = callback_client
    client.app.state.pending_id_token[0] = keys.sign_id_token(
        {"email": "member@example.test", "email_verified": True}
    )
    original_post = httpx.AsyncClient.post
    exchanges = []

    async def tracked_post(self, url, **kwargs):
        exchanges.append(kwargs["data"])
        return await original_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", tracked_post)
    flows = []
    for target in ("/c/first-session", "/c/second-session"):
        response = client.get("/auth/login", params={"return_to": target}, follow_redirects=False)
        name, value = next(iter(response.cookies.items()))
        payload = jwt.decode(value, _TEST_SECRET, algorithms=["HS256"])
        flows.append((name, payload, target))
    for index in order:
        name, payload, target = flows[index]
        response = client.get(
            "/auth/callback",
            params={"state": payload["state"], "code": f"code-{index}"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == target
        assert exchanges[-1]["code_verifier"] == payload["code_verifier"]
        assert name not in client.cookies
    assert len(exchanges) == 2


def test_unknown_callback_does_not_consume_other_pending_login(
    callback_client: tuple[TestClient, _IdpKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = callback_client
    login = client.get("/auth/login", follow_redirects=False)
    name, value = next(iter(login.cookies.items()))

    async def no_exchange(*args, **kwargs):
        pytest.fail("Unknown state must not exchange an authorization code")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_exchange)
    response = client.get("/auth/callback?state=unrelated&code=c")
    assert response.status_code == 400
    assert client.cookies[name] == value
    assert "ap_session" not in client.cookies


def test_flow_cookie_payload_must_match_callback_state(
    callback_client: tuple[TestClient, _IdpKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = callback_client
    login = client.get("/auth/login", follow_redirects=False)
    name, value = next(iter(login.cookies.items()))
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    payload = jwt.decode(value, _TEST_SECRET, algorithms=["HS256"])
    payload["state"] = "another-state"
    client.cookies.clear()
    client.cookies.set(name, jwt.encode(payload, _TEST_SECRET, algorithm="HS256"))

    async def no_exchange(*args, **kwargs):
        pytest.fail("Mismatched signed state must not exchange a code")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_exchange)
    response = client.get("/auth/callback", params={"state": state, "code": "c"})
    assert response.status_code == 400
    assert response.json()["error"] == "State mismatch (possible CSRF)"
    assert "ap_session" not in client.cookies


def test_abandoned_pending_cookies_are_bounded(
    callback_client: tuple[TestClient, _IdpKeys],
) -> None:
    client, _ = callback_client
    first_name = None
    for _ in range(12):
        response = client.get("/auth/login", follow_redirects=False)
        first_name = first_name or next(iter(response.cookies))
    names = [name for name in client.cookies if name.startswith("ap_auth_state_")]
    assert len(names) == 8
    assert first_name not in names
    assert "Max-Age=300" in response.headers["set-cookie"]
