"""Workload exchange, caching, rotation and failure containment."""

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from omnigent.server import openrouter_wif as wif


def _token(value="access-token", **overrides):
    return httpx.Response(
        200,
        json={
            "access_token": value,
            "expires_in": 900,
            "token_type": "Bearer",
            **overrides,
        },
    )


async def test_metadata_exchange_single_flight_and_renewal(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(wif, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            assert request.headers["Metadata-Flavor"] == "Google"
            assert request.url.params["audience"] == "https://openrouter.ai"
            return httpx.Response(200, text="google-identity")
        fields = parse_qs(request.content.decode())
        assert fields == {
            "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
            "subject_token_type": ["urn:ietf:params:oauth:token-type:jwt"],
            "federation_policy_id": ["policy"],
            "subject_token": ["google-identity"],
        }
        assert str(request.url) == "https://openrouter.ai/api/v1/oauth/token"
        return _token(f"access-{len(calls)}")

    broker = wif.OpenRouterWIFBroker(
        wif.OpenRouterWIFConfig("policy"), transport=httpx.MockTransport(handle)
    )
    responses = await asyncio.gather(*(broker.credential() for _ in range(10)))
    assert {r["token"] for r in responses} == {"access-2"}
    assert len(calls) == 2
    clock[0] += 781
    assert (await broker.credential())["token"] == "access-4"
    assert len(calls) == 4


async def test_projected_subject_is_reread_on_refresh(tmp_path, monkeypatch):
    path = tmp_path / "jwt"
    path.write_text("first-subject")
    subjects = []
    clock = [100.0]
    monkeypatch.setattr(wif, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))

    def handle(request):
        assert request.method == "POST"
        subjects.append(parse_qs(request.content.decode())["subject_token"][0])
        return _token()

    broker = wif.OpenRouterWIFBroker(
        wif.OpenRouterWIFConfig("policy", subject_token_file=str(path)),
        transport=httpx.MockTransport(handle),
    )
    await broker.credential()
    path.write_text("second-subject")
    clock[0] += 781
    await broker.credential()
    assert subjects == ["first-subject", "second-subject"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, text="secret-subject-and-token"),
        httpx.Response(302, headers={"Location": "https://untrusted.example"}),
        httpx.Response(200, json=[]),
        _token(expires_in=0),
        _token(expires_in=120),
        _token(expires_in=True),
        _token(expires_in=901),
        _token(expires_in="900"),
        _token(value=""),
        _token(token_type="unexpected"),
    ],
)
async def test_invalid_exchange_fails_closed_without_response_disclosure(response, tmp_path):
    path = tmp_path / "jwt"
    path.write_text("secret-subject")
    broker = wif.OpenRouterWIFBroker(
        wif.OpenRouterWIFConfig("policy", subject_token_file=str(path)),
        transport=httpx.MockTransport(lambda _: response),
    )
    with pytest.raises(wif.OpenRouterWIFError) as caught:
        await broker.credential()
    assert str(caught.value) == "OpenRouter workload identity exchange failed"
    assert not broker._token


async def test_failed_renewal_does_not_return_cached_token(tmp_path, monkeypatch):
    path = tmp_path / "jwt"
    path.write_text("subject")
    clock = [100.0]
    monkeypatch.setattr(wif, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    responses = iter([_token(), httpx.Response(403), _token("renewed")])
    broker = wif.OpenRouterWIFBroker(
        wif.OpenRouterWIFConfig("policy", subject_token_file=str(path)),
        transport=httpx.MockTransport(lambda _: next(responses)),
    )
    await broker.credential()
    clock[0] += 901
    with pytest.raises(wif.OpenRouterWIFError):
        await broker.credential()
    clock[0] += 5
    assert (await broker.credential())["token"] == "renewed"


@pytest.mark.parametrize(
    "config",
    [
        {},
        [],
        {"policy_id": ""},
        {"policy_id": 1},
        {"policy_id": "p", "audience": ""},
        {"policy_id": "p", "subject_token_file": "relative"},
        {"policy_id": "p", "typo": True},
    ],
)
def test_invalid_config_rejected(config):
    with pytest.raises(ValueError, match="openrouter_wif"):
        wif.OpenRouterWIFConfig.parse(config)


def test_disabled_and_default_config():
    assert wif.OpenRouterWIFConfig.parse(None) is None
    assert wif.OpenRouterWIFConfig.parse({"policy_id": "policy"}) == wif.OpenRouterWIFConfig(
        "policy"
    )


async def test_outage_is_shared_with_waiters_and_recovers_after_backoff(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(wif, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    calls = []
    failing = True
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        calls.append(request)
        if failing:
            entered.set()
            await release.wait()
            raise httpx.ReadTimeout("private upstream details")
        if request.method == "GET":
            return httpx.Response(200, text="identity")
        return _token("recovered")

    broker = wif.OpenRouterWIFBroker(
        wif.OpenRouterWIFConfig("policy"), transport=httpx.MockTransport(handle)
    )
    first = asyncio.create_task(broker.credential())
    await entered.wait()
    waiters = [asyncio.create_task(broker.credential()) for _ in range(10)]
    release.set()
    results = await asyncio.gather(first, *waiters, return_exceptions=True)
    assert all(isinstance(result, wif.OpenRouterWIFError) for result in results)
    assert len(calls) == 1
    clock[0] += 4
    with pytest.raises(wif.OpenRouterWIFError):
        await broker.credential()
    assert len(calls) == 1
    failing = False
    clock[0] += 1
    assert (await broker.credential())["token"] == "recovered"
    assert len(calls) == 3


@pytest.mark.parametrize("explicit_config", [None, {}])
def test_app_uses_file_config_unless_explicit_config_supplied(
    tmp_path, db_uri, monkeypatch, explicit_config
):
    import time

    from fastapi.testclient import TestClient

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    config = tmp_path / "server.yaml"
    config.write_text("openrouter_wif:\n  policy_id: file-policy\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    calls = []

    async def credential(broker):
        calls.append(broker.config.policy_id)
        return {"token": "test-inference-token", "expires_in": 800}

    monkeypatch.setattr(wif.OpenRouterWIFBroker, "credential", credential)
    hosts = HostStore(db_uri)
    host_id = "a" * 32
    hosts.register_managed_host(
        host_id=host_id,
        name="test",
        user_id="test@example.com",
        token="launch-token",
        provider="test",
        sandbox_id="test",
        token_expires_at=int(time.time()) + 600,
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifacts, tmp_path / "cache"),
        host_store=hosts,
        server_config=explicit_config,
    )
    response = TestClient(app).get(
        f"/v1/hosts/{host_id}/credentials/openrouter",
        headers={"X-Omnigent-Host-Token": "launch-token"},
    )
    assert response.status_code == (200 if explicit_config is None else 404)
    assert calls == (["file-policy"] if explicit_config is None else [])
