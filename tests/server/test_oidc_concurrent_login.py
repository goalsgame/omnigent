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


def test_legacy_cookie_counts_toward_pending_flow_limit(callback_client) -> None:
    client, _ = callback_client
    client.cookies.set(
        "ap_auth_state",
        jwt.encode({"state": "old-login", "exp": 4102444800}, _TEST_SECRET, algorithm="HS256"),
        domain="testserver.local",
        path="/",
    )
    for _ in range(8):
        client.get("/auth/login", follow_redirects=False)
    names = [
        name
        for name in client.cookies
        if name == "ap_auth_state" or name.startswith("ap_auth_state_")
    ]
    assert len(names) == 8
    assert "ap_auth_state" not in names


@pytest.mark.parametrize("callback_client", [{"flow_state_cookies": False}], indirect=True)
def test_compatibility_mode_emits_legacy_cookie_readable_by_old_handlers(callback_client) -> None:
    client, keys = callback_client
    client.app.state.pending_id_token[0] = keys.sign_id_token(
        {"email": "member@example.test", "email_verified": True}
    )
    login = client.get(
        "/auth/login", params={"return_to": "/c/compatible"}, follow_redirects=False
    )
    assert list(login.cookies) == ["ap_auth_state"]
    payload = jwt.decode(login.cookies["ap_auth_state"], _TEST_SECRET, algorithms=["HS256"])
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    assert payload["state"] == state
    assert payload["code_verifier"]
    response = client.get(
        "/auth/callback", params={"state": state, "code": "c"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/c/compatible"
    assert "ap_auth_state" not in client.cookies


@pytest.mark.parametrize(
    ("other_cookie_bytes", "path_length"), [(0, 1800), (2200, 1800), (5000, 100)]
)
def test_large_return_paths_fit_the_aggregate_cookie_budget(
    callback_client, other_cookie_bytes, path_length
) -> None:
    client, keys = callback_client
    client.app.state.pending_id_token[0] = keys.sign_id_token(
        {"email": "member@example.test", "email_verified": True}
    )
    if other_cookie_bytes:
        client.cookies.set("other", "x" * other_cookie_bytes, domain="testserver.local", path="/")
    last = None
    for index in range(6):
        target = "/c/" + "x" * path_length + str(index)
        login = client.get("/auth/login", params={"return_to": target}, follow_redirects=False)
        assert login.status_code == 302
        cookie_header = client.build_request("GET", "/auth/callback").headers.get("cookie", "")
        assert len(cookie_header.encode()) <= 6144
        name, value = next((name, value) for name, value in login.cookies.items() if value)
        assert len(f"{name}={value}; ".encode()) <= 3800
        payload = jwt.decode(value, _TEST_SECRET, algorithms=["HS256"])
        assert payload["return_to"] == target
        last = (payload["state"], target)
    assert last is not None
    callback = client.get(
        "/auth/callback", params={"code": "c", "state": last[0]}, follow_redirects=False
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == last[1]


def test_oversized_state_is_rejected_without_discarding_pending_login(callback_client) -> None:
    client, _ = callback_client
    login = client.get("/auth/login", follow_redirects=False)
    name, value = next(iter(login.cookies.items()))
    response = client.get(
        "/auth/login", params={"return_to": "/" + "x" * 5000}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "too large" in response.json()["error"]
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert not response.cookies
    assert client.cookies[name] == value


def test_large_other_cookies_leave_no_room_for_new_state(callback_client) -> None:
    client, _ = callback_client
    client.cookies.set("other", "x" * 6000, domain="testserver.local", path="/")
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 400
    assert not response.cookies
    assert "location" not in response.headers
