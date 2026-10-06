"""Scoped installation tokens, policy validation and live machine revocation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omnigent.server.github_app import GitHubAppConfig, GitHubAppError
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.github_machine import GitHubMachineBroker, parse_machine_bindings

PRINCIPAL = "oidc-machine:ticket-bot"
POLICY = {"installation_id": 123, "repository_ids": [456], "access": "write"}


def bindings(raw=POLICY):
    return parse_machine_bindings({PRINCIPAL: raw}, principals=frozenset({PRINCIPAL}))


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {**POLICY, "repository_ids": []},
        {**POLICY, "repository_ids": [True]},
        {**POLICY, "repository_ids": [456, 456]},
        {**POLICY, "repository_ids": [0]},
        {**POLICY, "repository_ids": list(range(1, 502))},
        {**POLICY, "installation_id": True},
        {**POLICY, "installation_id": 0},
        {**POLICY, "access": "admin"},
        {**POLICY, "ci_read": "true"},
        {**POLICY, "extra": "ignored"},
    ],
)
def test_invalid_policy_fails_closed(raw):
    with pytest.raises(RuntimeError):
        bindings(raw)


def test_human_or_unknown_machine_cannot_receive_installation_policy():
    for principal in ("alice@example.com", "oidc-machine:unknown"):
        with pytest.raises(RuntimeError):
            parse_machine_bindings({principal: POLICY}, principals=frozenset({PRINCIPAL}))
    assert parse_machine_bindings(None, principals=frozenset()) == {}


def client(handler, monkeypatch):
    config = GitHubAppConfig("123", "client", "secret", "key", "https://example.com/cb", "app")
    monkeypatch.setattr(GitHubAppConfig, "mint_app_jwt", lambda self: "signed-app-jwt")
    return GitHubAppClient(config, transport=httpx.MockTransport(handler))


def payload():
    return {
        "token": "ghs_test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


@pytest.mark.asyncio
async def test_installation_request_always_limits_repositories_and_permissions(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(201, json=payload())

    broker = GitHubMachineBroker(bindings(), client(handle, monkeypatch), lambda p: p == PRINCIPAL)
    first, second = await asyncio.gather(broker.resolve(PRINCIPAL), broker.resolve(PRINCIPAL))
    assert first == second and first["token"] == "ghs_test"
    assert first["login"] == "app[bot]"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.github.com/app/installations/123/access_tokens"
    assert requests[0].headers["Authorization"] == "Bearer signed-app-jwt"
    assert json.loads(requests[0].content) == {
        "repository_ids": [456],
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    }
    assert await broker.resolve("alice@example.com") is None
    assert await broker.resolve("oidc-machine:unknown") is None


@pytest.mark.asyncio
async def test_ci_read_requests_only_read_permissions(monkeypatch):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, json=payload())

    broker = GitHubMachineBroker(
        bindings({**POLICY, "ci_read": True}), client(handle, monkeypatch), lambda p: True
    )
    assert await broker.resolve(PRINCIPAL) is not None
    assert requests == [
        {
            "repository_ids": [456],
            "permissions": {
                "contents": "write",
                "pull_requests": "write",
                "metadata": "read",
                "checks": "read",
                "actions": "read",
            },
        }
    ]


@pytest.mark.asyncio
async def test_cached_tokens_do_not_bypass_live_principal_checks(monkeypatch):
    allowed = True
    broker = GitHubMachineBroker(
        bindings(),
        client(lambda r: httpx.Response(201, json=payload()), monkeypatch),
        lambda p: allowed,
    )
    assert await broker.resolve(PRINCIPAL) is not None
    allowed = False
    assert await broker.resolve(PRINCIPAL) is None


@pytest.mark.asyncio
async def test_revocation_during_mint_does_not_vend_token(monkeypatch):
    allowed = True

    def handle(request):
        nonlocal allowed
        allowed = False
        return httpx.Response(201, json=payload())

    broker = GitHubMachineBroker(bindings(), client(handle, monkeypatch), lambda p: allowed)
    assert await broker.resolve(PRINCIPAL) is None
    assert not broker._tokens


@pytest.mark.asyncio
async def test_expiring_token_refreshes_with_same_scope(monkeypatch):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, json=payload())

    broker = GitHubMachineBroker(
        bindings({**POLICY, "access": "read"}), client(handle, monkeypatch), lambda p: True
    )
    await broker.resolve(PRINCIPAL)
    broker._tokens[PRINCIPAL] = ("old", datetime.now(timezone.utc).timestamp() + 60)
    assert (await broker.resolve(PRINCIPAL))["token"] == "ghs_test"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]["permissions"]["contents"] == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (403, {}),
        (201, {}),
        (201, {"token": "bad", "expires_at": "invalid"}),
        (201, {"token": "", "expires_at": "2099-01-01T00:00:00Z"}),
        (201, {"token": "expired", "expires_at": "2000-01-01T00:00:00Z"}),
        (201, {"token": "naive", "expires_at": "2099-01-01T00:00:00"}),
    ],
)
async def test_invalid_or_denied_token_response_is_not_cached(monkeypatch, status, body):
    broker = GitHubMachineBroker(
        bindings(),
        client(lambda r: httpx.Response(status, json=body), monkeypatch),
        lambda p: True,
    )
    with pytest.raises(GitHubAppError):
        await broker.resolve(PRINCIPAL)
    assert not broker._tokens


@pytest.mark.asyncio
async def test_slow_mint_does_not_block_other_principals(monkeypatch):
    principals = [f"oidc-machine:bot-{n}" for n in range(3)]
    policy = parse_machine_bindings(
        {p: {**POLICY, "installation_id": n + 1} for n, p in enumerate(principals)},
        principals=frozenset(principals),
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/1/access_tokens"):
            started.set()
            await release.wait()
        return httpx.Response(201, json=payload())

    broker = GitHubMachineBroker(policy, client(handle, monkeypatch), lambda p: True)
    cached = await broker.resolve(principals[1])
    slow = asyncio.create_task(broker.resolve(principals[0]))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await asyncio.wait_for(broker.resolve(principals[1]), timeout=2) == cached
        assert await asyncio.wait_for(broker.resolve(principals[2]), timeout=2) is not None
        assert len(calls) == 3
    finally:
        release.set()
        await asyncio.wait_for(slow, timeout=2)
