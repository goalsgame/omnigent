"""E2E: the OIDC/SSO login journey, driven headlessly against a fake IdP.

An unauthenticated SPA navigation in OIDC mode bounces to the IdP's sign-in
page and, after the user authenticates, lands back in the app authenticated.
Deployments use a real SSO provider, so this journey can't be filmed against
the live app — the recorder would be bounced to real SSO. This test wires the
server to a fake in-process IdP (:mod:`tests.e2e_ui.auth._fake_idp`) so the
whole redirect chain runs locally and headlessly:

    SPA → /v1/me 401 (login_url) → /auth/login → 302 fake IdP /authorize
        → "Continue" → /auth/callback (code+state, signed id_token) → app

This is the reproduction lane for auth/OIDC login bugs (T3): the recorded video
shows the real sign-in page and the authenticated landing, not a proxy.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server


@pytest.fixture(
    scope="module", params=[False, True], ids=["discovery-confidential", "explicit-public-ps256"]
)
def oidc_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[OIDCServer]:
    """A dedicated OIDC-mode server wired to a fake IdP."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_oidc_login")
    yield from spawn_oidc_server(mock_llm_server_url, server_tmp, public_client=request.param)


def test_oidc_login_redirects_through_idp_to_authenticated_app(
    oidc_server: OIDCServer, page: Page
) -> None:
    """Navigating the SPA unauthenticated lands on the IdP sign-in page;
    continuing there returns to the app authenticated.

    Drives the full OIDC redirect chain against the fake IdP and asserts both
    ends a user observes: the sign-in page (naming the identity) and the
    authenticated app shell (the composer).
    """
    # 1. Land on the SPA unauthenticated. The client probes /v1/me, gets a 401
    #    with login_url, and redirects the browser to /auth/login, which 302s
    #    to the fake IdP's /authorize page. Playwright follows the 302s.
    page.goto(oidc_server.public_url)

    # 2. The fake IdP sign-in page is shown, naming the identity to sign in as.
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    expect(continue_link).to_contain_text(oidc_server.idp.email)

    # 3. Continue through the IdP → /auth/callback exchanges the code for a
    #    signed id_token, mints the session cookie, and 302s back to the app.
    continue_link.click()

    # 4. Back in the authenticated app: no longer on any auth page, and the
    #    app shell (the sidebar, which renders only for an authenticated
    #    session) is shown. Assert on the shell rather than the composer, since
    #    the post-login landing route need not be a chat with a composer.
    expect(page).not_to_have_url(re.compile(r"/authorize|/auth/login"), timeout=15_000)
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)


def test_oidc_cli_ticket_completes_through_browser(oidc_server: OIDCServer, page: Page) -> None:
    """The CLI's browser-ticket flow authenticates against either provider profile."""
    response = page.request.post(f"{oidc_server.base_url}/auth/cli-login")
    assert response.status == 200
    ticket = response.json()
    page.goto(f"{oidc_server.public_url}{ticket['login_url']}")
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    continue_link.click()
    expect(page.get_by_role("heading", name="Login successful")).to_be_visible(timeout=15_000)
    poll = page.request.get(
        f"{oidc_server.base_url}/auth/cli-poll", params={"ticket": ticket["ticket"]}
    )
    assert poll.status == 200
    assert poll.json()["user_id"] == oidc_server.idp.email
    assert poll.json()["token"]
    replay = page.request.get(
        f"{oidc_server.base_url}/auth/cli-poll", params={"ticket": ticket["ticket"]}
    )
    assert replay.status == 410


@pytest.mark.parametrize("drop_callback_cookie", [False, True], ids=["normal", "cookie-retry"])
def test_oidc_deep_link_survives_first_callback_cookie_omission(
    oidc_server: OIDCServer, page: Page, drop_callback_cookie: bool
) -> None:
    """A one-request cookie omission must not strand a signed-in deep link."""
    callbacks: list[str] = []

    def callback(route: Route) -> None:
        callbacks.append(route.request.url)
        if drop_callback_cookie and len(callbacks) == 1:
            callback_url = urlsplit(route.request.url)
            response = route.fetch(
                url=f"{oidc_server.base_url}{callback_url.path}?{callback_url.query}",
                headers={**route.request.all_headers(), "cookie": ""},
                max_redirects=0,
            )
            assert response.status == 200
            assert response.headers["content-type"].startswith("text/html")
            route.fulfill(response=response)
        else:
            route.continue_()

    page.route("**/auth/callback?**", callback)
    target = "/c/shared-chat?tab=files"
    page.goto(f"{oidc_server.public_url}{target}")
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    continue_link.click()
    expect(page).to_have_url(f"{oidc_server.public_url}{target}", timeout=15_000)
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
    identity = page.evaluate(
        "async () => { const r = await fetch('/v1/me'); "
        "return {status: r.status, body: await r.json()}; }"
    )
    assert identity["status"] == 200
    assert identity["body"]["user_id"] == oidc_server.idp.email
    assert len(callbacks) == (2 if drop_callback_cookie else 1)
    if drop_callback_cookie:
        first, second = [parse_qs(urlsplit(url).query) for url in callbacks]
        assert first["code"] == second["code"]
        assert first["state"] == second["state"]


def test_oidc_missing_cookie_does_not_loop_or_authenticate(
    oidc_server: OIDCServer, page: Page
) -> None:
    """A browser that omits the cookie on both attempts gets a bounded error."""
    callbacks: list[str] = []

    def callback(route: Route) -> None:
        callbacks.append(route.request.url)
        callback_url = urlsplit(route.request.url)
        response = route.fetch(
            url=f"{oidc_server.base_url}{callback_url.path}?{callback_url.query}",
            headers={**route.request.all_headers(), "cookie": ""},
            max_redirects=0,
        )
        route.fulfill(response=response)

    page.route("**/auth/callback?**", callback)
    page.goto(oidc_server.public_url)
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    continue_link.click()
    expect(page.get_by_role("heading", name="Sign-in could not finish")).to_be_visible(
        timeout=15_000
    )
    assert len(callbacks) == 2
    assert page.locator("script").count() == 0
    assert not any(cookie["name"] == "ap_session" for cookie in page.context.cookies())
    identity = page.request.get(f"{oidc_server.base_url}/v1/me")
    assert identity.status == 401
