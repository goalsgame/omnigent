"""Host relay that gives a managed sandbox its owner's Go proxy token."""

import json
import os

import httpx
import pytest

from omnigent import goproxy_credential as goproxy
from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

URL = "https://go.example.test"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-token")
    monkeypatch.setenv("IS_SANDBOX", "1")
    return tmp_path


def reply(status=200, **body):
    return httpx.Response(status, request=httpx.Request("GET", "https://server.test"), json=body)


def test_credential_sends_the_host_token_and_url(monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return reply(connected=True, token="abc", url=URL, prefix="go.example.test", extra="x")

    monkeypatch.setattr(httpx, "get", get)
    result = goproxy.credential("https://server.test/", "host 1", URL, host_token="host-token")
    assert result == {"token": "abc", "url": URL, "prefix": "go.example.test"}
    assert seen["url"] == "https://server.test/v1/hosts/host%201/credentials/goproxy"
    assert seen["params"] == {"url": URL}
    assert seen["headers"] == {MANAGED_HOST_TOKEN_HEADER: "host-token"}


@pytest.mark.parametrize(
    "response",
    [
        reply(403, detail="SECRET"),
        reply(connected=False),
        reply(connected=True),
        reply(connected=True, token="-flag"),
        reply(connected=True, token="two words"),
    ],
)
def test_credential_errors_are_sanitized(monkeypatch, response):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)
    with pytest.raises(goproxy.GoProxyAccessError) as exc:
        goproxy.credential("https://server.test", "host", URL, host_token="host-token")
    assert "SECRET" not in str(exc.value)


def test_local_host_is_untouched(home, monkeypatch):
    monkeypatch.delenv("IS_SANDBOX")
    goproxy.configure("https://server.test", "host")
    assert not (home / ".omnigent").exists()


def test_no_host_token_means_no_relay(home, monkeypatch):
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR)
    goproxy.configure("https://server.test", "host")
    assert not goproxy.config_path().exists()


def test_relay_serves_only_marked_requests_for_a_url(home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        goproxy,
        "credential",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"token": "abc", "url": URL},
    )
    goproxy.configure("https://server.test", "host")
    path = goproxy.config_path()
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    relay_url = json.loads(path.read_text())["relay_url"]
    assert relay_url.startswith("http://127.0.0.1:")

    def get(params, headers):
        return httpx.get(relay_url, params=params, headers=headers, trust_env=False)

    marked = {goproxy.RELAY_HEADER: "1"}
    assert get({"url": URL}, {}).status_code == 403
    assert get({}, marked).status_code == 403
    assert get({"url": URL, "x": "y"}, marked).status_code == 403
    assert get({"url": URL, "x": "1", "y": "2"}, marked).status_code == 403
    assert calls == []
    ok = get({"url": URL}, marked)
    assert ok.status_code == 200
    assert ok.json() == {"connected": True, "token": "abc", "url": URL}
    assert ok.headers["cache-control"] == "no-store"
    assert calls == [(("https://server.test", "host", URL), {"host_token": "host-token"})]


def test_relay_hides_server_failures(home, monkeypatch):
    def fail(*args, **kwargs):
        raise goproxy.GoProxyAccessError("SECRET")

    monkeypatch.setattr(goproxy, "credential", fail)
    goproxy.configure("https://server.test", "host")
    relay_url = json.loads(goproxy.config_path().read_text())["relay_url"]
    response = httpx.get(
        relay_url, params={"url": URL}, headers={goproxy.RELAY_HEADER: "1"}, trust_env=False
    )
    assert response.status_code == 403
    assert "SECRET" not in response.text
    assert os.environ["IS_SANDBOX"] == "1"
