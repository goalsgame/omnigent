"""A callback document restores cookies without bypassing state validation."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from tests.server.test_oidc_callback import _TEST_SECRET, _IdpKeys, callback_client

__all__ = ["callback_client"]


def _retry_url(response: httpx.Response) -> str:
    match = re.search(r'<a id="continue-sign-in" href="([^"]+)"', response.text)
    assert match is not None
    return unescape(match[1])


@pytest.mark.parametrize(
    ("accept", "html"),
    [
        ("application/json, text/html;q=0", False),
        ("text/html;q=0.000", False),
        ("*/*", False),
        ("text/*", False),
        ("text/html-suffix", False),
        ("text/html;q=invalid", False),
        ("text/html;q=-0.5", False),
        ("text/html;q=1.1", False),
        ("text/html;q=NaN", False),
        ("text/html;q=inf", False),
        ("text/html;q=+1", False),
        ("text/html;q=1e0", False),
        ("text/html;q=0.0001", False),
        ("text/html;q=1;q=0", False),
        ('text/html;profile="a,b";q=0', False),
        ('text/html;profile="a;q=1";q=0', False),
        ('application/json;profile="text/html,a,b";q=1', False),
        ('application/json;profile="a,b",text/html;q=0', False),
        ('text/html;profile="unterminated', False),
        (r'text/html;profile="a\"', False),
        ("text/html", True),
        ("TEXT/HTML", True),
        ("Text/Html ; Q=0.5", True),
        ("application/json, text/html;q=0.2", True),
        ('text/html;profile="a,b;c";q=0.5', True),
        (r'text/html;profile="a\";q=0,b";q=1', True),
        (r'text/html;profile="a\\",application/json', True),
        ('application/json;profile="a,b", TEXT/HTML;Q=0.5', True),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
    ],
)
def test_recovery_document_requires_explicit_acceptable_html(
    callback_client: tuple[TestClient, _IdpKeys], accept: str, html: bool
) -> None:
    client, _ = callback_client
    response = client.get("/auth/callback?code=c&state=s", headers={"Accept": accept})
    assert response.status_code == (200 if html else 400)
    assert response.headers["content-type"].startswith("text/html" if html else "application/json")
    assert not response.cookies


def test_login_redirect_does_not_cache_auth_state(
    callback_client: tuple[TestClient, _IdpKeys],
) -> None:
    client, _ = callback_client
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert any(name.startswith("ap_auth_state_") for name in response.cookies)


def test_missing_cookie_document_recovers_original_link(
    callback_client: tuple[TestClient, _IdpKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, keys = callback_client
    client.app.state.pending_id_token[0] = keys.sign_id_token(
        {"email": "member@example.test", "email_verified": True}
    )
    original_post = httpx.AsyncClient.post
    exchanges: list[dict[str, str]] = []

    async def tracked_post(self, url, **kwargs):
        exchanges.append(kwargs["data"])
        return await original_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", tracked_post)
    target = "/c/shared-session?tab=files"
    login = client.get("/auth/login", params={"return_to": target}, follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    cookie_name = next(iter(login.cookies))
    cookie = login.cookies[cookie_name]
    client.cookies.clear()
    callback = client.get(
        "/auth/callback",
        params={"code": "original-code", "state": state},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert callback.status_code == 200
    assert callback.headers["content-type"].startswith("text/html")
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert "nonce-" in callback.headers["content-security-policy"]
    assert not callback.cookies
    assert exchanges == []
    retry = _retry_url(callback)
    query = parse_qs(urlsplit(retry).query)
    assert query["code"] == ["original-code"]
    assert query["state"] == [state]
    assert urlsplit(retry).netloc == ""
    client.cookies.set(cookie_name, cookie)
    recovered = client.get(retry, headers={"Accept": "text/html"}, follow_redirects=False)
    assert recovered.status_code == 302
    assert recovered.headers["location"] == target
    assert (
        jwt.decode(recovered.cookies["ap_session"], _TEST_SECRET, algorithms=["HS256"])["sub"]
        == "member@example.test"
    )
    assert len(exchanges) == 1
    assert exchanges[0]["code"] == "original-code"
    assert (
        exchanges[0]["code_verifier"]
        == jwt.decode(cookie, _TEST_SECRET, algorithms=["HS256"])["code_verifier"]
    )


def test_missing_cookie_retry_stops_without_authenticating(
    callback_client: tuple[TestClient, _IdpKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = callback_client

    async def no_exchange(*args, **kwargs):
        pytest.fail("A missing cookie must never redeem a code")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_exchange)
    first = client.get("/auth/callback?code=c&state=s", headers={"Accept": "text/html"})
    retry = client.get(_retry_url(first), headers={"Accept": "text/html"})
    assert retry.status_code == 400
    assert "Sign-in could not finish" in retry.text
    assert "window.location.replace" not in retry.text
    assert not retry.cookies


@pytest.mark.parametrize("state_cookie", [None, "forged-cookie"])
def test_non_browser_missing_cookie_remains_an_error(
    callback_client: tuple[TestClient, _IdpKeys], state_cookie: str | None
) -> None:
    client, _ = callback_client
    if state_cookie:
        client.cookies.set("ap_auth_state", state_cookie)
    response = client.get("/auth/callback?code=c&state=s", headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert not response.cookies


@pytest.mark.parametrize("cookie_state", ["different-state", None])
def test_retry_does_not_bypass_invalid_cookie_or_state(
    callback_client: tuple[TestClient, _IdpKeys],
    monkeypatch: pytest.MonkeyPatch,
    cookie_state: str | None,
) -> None:
    client, _ = callback_client
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    cookie_name = next(iter(login.cookies))
    cookie = login.cookies[cookie_name]
    client.cookies.clear()
    first = client.get(
        "/auth/callback", params={"code": "c", "state": state}, headers={"Accept": "text/html"}
    )
    if cookie_state:
        payload = jwt.decode(cookie, _TEST_SECRET, algorithms=["HS256"])
        payload["state"] = cookie_state
        cookie = jwt.encode(payload, _TEST_SECRET, algorithm="HS256")
    else:
        cookie = "forged-cookie"
    client.cookies.set(cookie_name, cookie)

    async def no_exchange(*args, **kwargs):
        pytest.fail("An invalid cookie or mismatched state must never redeem a code")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_exchange)
    response = client.get(_retry_url(first), headers={"Accept": "text/html"})
    assert response.status_code == 400
    assert "ap_session" not in response.cookies


def test_retry_url_cannot_inject_markup_or_an_external_redirect(
    callback_client: tuple[TestClient, _IdpKeys],
) -> None:
    client, _ = callback_client
    attack = '"><script>alert(1)</script>'
    response = client.get(
        "/auth/callback", params={"code": attack, "state": attack}, headers={"Accept": "text/html"}
    )
    assert attack not in response.text
    retry = urlsplit(_retry_url(response))
    assert not retry.netloc
    assert retry.path == "/auth/callback"
    assert parse_qs(retry.query)["code"] == [attack]
