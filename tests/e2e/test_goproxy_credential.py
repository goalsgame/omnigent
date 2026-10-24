"""A managed sandbox obtains its owner's Go proxy token through the host relay.

Only the identity provider is mocked: the sandbox client, host relay, real
host-credentials route and Go proxy broker run together, and the sandbox has no
Lore checkout and no launch token.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent import goproxy_credential
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.server.goproxy import GoProxyBroker, GoProxyConfig
from omnigent.server.routes.host_credentials import create_host_credentials_router
from tests.server import test_oidc_mcp
from tests.server.routes.test_host_credentials import _FakeHostStore

PROXY = "https://go.example.test"


@pytest.fixture
def sandbox(tmp_path: Path, db_uri, monkeypatch):
    base, claims, exchanges = test_oidc_mcp.broker.__wrapped__(db_uri, monkeypatch)
    broker = GoProxyBroker(
        GoProxyConfig.parse({"url": PROXY, "audience": "goproxy", "scope": "openid goproxy"}),
        base.oidc,
        base.store,
        base.auth,
        base.client,
    )
    broker.login_broker = SimpleNamespace(
        state=lambda owner: {
            "subject": "person-sub",
            "generation": "gen-1",
            "access_token": "login-token",
        },
        active=lambda owner, generation: True,
    )
    app = FastAPI()
    app.state.goproxy_identity_broker = broker
    app.include_router(
        create_host_credentials_router(
            _FakeHostStore("host1", "launch-token", "alice@example.com")
        ),
        prefix="/v1",
    )
    client = TestClient(app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            response = client.get(self.path, headers=dict(self.headers))
            self.send_response(response.status_code)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-token")
    goproxy_credential.configure(f"http://127.0.0.1:{server.server_port}", "host1")
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR)
    yield SimpleNamespace(home=home, exchanges=exchanges, claims=claims, broker=broker)
    server.shutdown()
    server.server_close()
    client.close()


def ask(home: Path, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    relay_url = json.loads((home / ".omnigent/goproxy/host.json").read_text())["relay_url"]
    return httpx.get(
        relay_url,
        params={"url": url},
        headers={goproxy_credential.RELAY_HEADER: "1"} if headers is None else headers,
        trust_env=False,
    )


def test_sandbox_without_lore_gets_its_owners_proxy_token(sandbox):
    assert not (sandbox.home / ".omnigent/lore").exists()
    assert "launch-token" not in (sandbox.home / ".omnigent/goproxy/host.json").read_text()
    response = ask(sandbox.home, PROXY)
    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "token": "access",
        "url": PROXY,
        "prefix": "go.example.test",
    }
    assert sandbox.exchanges[-1]["scope"] == ["openid goproxy"]
    assert sandbox.exchanges[-1]["subject_token"] == ["login-token"]
    assert len(sandbox.exchanges) == 1
    assert ask(sandbox.home, PROXY).json()["token"] == "access"
    assert len(sandbox.exchanges) == 1


def test_relay_refuses_other_urls_and_unmarked_requests(sandbox):
    assert ask(sandbox.home, "https://evil.example.test").status_code == 403
    assert ask(sandbox.home, PROXY, headers={}).status_code == 403
    assert sandbox.exchanges == []


def test_failed_exchange_gives_the_sandbox_nothing(sandbox):
    sandbox.claims["azp"] = "someone-else"
    response = ask(sandbox.home, PROXY)
    assert response.status_code == 403
    assert "access" not in response.text
