"""Validate server-owned routes and ADC rotation without live credentials."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from omnigent.server.inference_upstreams import (
    GoogleInferenceCredential,
    InferenceUpstream,
    parse_upstreams,
)


@pytest.mark.parametrize(
    "value",
    [
        {
            "kind": "openai",
            "base_url": "http://inference.example",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://user:pass@inference.example",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://inference.example:99999",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://inference.example/v1?token=secret",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://inference.example/v1#fragment",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://inference.example/../admin",
            "api_key_env": "ROUTING_TEST_KEY",
        },
        {
            "kind": "openai",
            "base_url": "https://inference.example",
            "api_key_env": "not an env name",
        },
        {"kind": "openrouter", "base_url": "https://attacker.example"},
        {"kind": "vertex-openai", "project": "../other", "location": "global"},
        {"kind": "vertex-anthropic", "project": "example-project", "location": "eu/../../other"},
        {
            "kind": "vertex-openai",
            "project": "example-project",
            "location": "global",
            "base_url": "https://attacker.example",
        },
        {"kind": "typo"},
    ],
)
def test_untrusted_or_ambiguous_configuration_is_rejected(monkeypatch, value):
    monkeypatch.setenv("ROUTING_TEST_KEY", "test-key")
    with pytest.raises(ValueError):
        InferenceUpstream.parse(value)


@pytest.mark.parametrize(
    "patch",
    [
        {"model_routes": {}},
        {"model_routes": {"alias": {"upstream": "unknown", "model": "target"}}},
        {
            "model_routes": {
                "alias": {
                    "upstream": "route",
                    "model": "target",
                    "url": "https://attacker.example",
                }
            }
        },
        {"model_routes": {"alias": {"upstream": "route", "model": ""}}},
        {
            "model_routes": {
                "alias": {"upstream": "route", "model": "target"},
                "hidden": {"upstream": "route", "model": "target"},
            }
        },
        {"upstreams": {}},
        {"upstreams": {"bad/name": {"kind": "openrouter"}}},
    ],
)
def test_routes_must_cover_exact_allowlist(patch):
    raw = {
        "upstreams": {"route": {"kind": "openrouter"}},
        "model_routes": {"alias": {"upstream": "route", "model": "target"}},
    }
    with pytest.raises(ValueError):
        parse_upstreams({**raw, **patch}, ("alias",))


def test_legacy_configuration_keeps_openrouter_default():
    assert parse_upstreams({}, ("alias",)) == ({}, {})


@pytest.mark.parametrize(
    ("location", "hostname"),
    [
        ("global", "aiplatform.googleapis.com"),
        ("eu", "aiplatform.eu.rep.googleapis.com"),
        ("us", "aiplatform.us.rep.googleapis.com"),
        ("europe-west1", "europe-west1-aiplatform.googleapis.com"),
    ],
)
def test_vertex_credentials_can_only_target_derived_google_hosts(location, hostname):
    upstream = InferenceUpstream.parse(
        {"kind": "vertex-openai", "project": "example-project", "location": location}
    )
    assert (
        upstream.base_url == f"https://{hostname}/v1/projects/example-project/locations/{location}"
    )


@pytest.mark.asyncio
async def test_adc_refresh_is_cached_and_serialized(monkeypatch):
    import google.auth

    calls = []
    credentials = SimpleNamespace(valid=False, token=None)

    def refresh(request):
        calls.append("refresh")
        time.sleep(0.01)
        credentials.token = "google-access-token"
        credentials.valid = True

    credentials.refresh = refresh

    def default(*, scopes):
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return credentials, "credential-project"

    monkeypatch.setattr(google.auth, "default", default)
    broker = GoogleInferenceCredential()
    assert (
        await asyncio.gather(*(broker.credential() for _ in range(8)))
        == ["google-access-token"] * 8
    )
    assert calls == ["refresh"]
    credentials.valid = False
    assert await broker.credential() == "google-access-token"
    assert calls == ["refresh", "refresh"]


@pytest.mark.asyncio
async def test_cancellation_cannot_start_concurrent_adc_refresh(monkeypatch):
    import google.auth

    started, release = threading.Event(), threading.Event()
    credentials = SimpleNamespace(valid=False, token=None)
    calls = []

    def refresh(request):
        calls.append("refresh")
        started.set()
        assert release.wait(3)
        credentials.token = "google-access-token"
        credentials.valid = True

    credentials.refresh = refresh
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (credentials, None))
    broker = GoogleInferenceCredential()
    first = asyncio.create_task(broker.credential())
    assert await asyncio.to_thread(started.wait, 3)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(broker.credential())
    release.set()
    assert await second == "google-access-token"
    assert calls == ["refresh"]


def test_refresh_waiters_do_not_starve_executor_after_cancellation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import google.auth

    started, release = threading.Event(), threading.Event()
    credentials = SimpleNamespace(valid=False, token=None)
    calls = []

    def refresh(request):
        calls.append("refresh")
        started.set()
        assert release.wait(5)
        credentials.token = "google-access-token"
        credentials.valid = True

    credentials.refresh = refresh
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (credentials, None))

    async def probe():
        # A private loop owns this bounded default executor; other tests and
        # their database fixtures retain their own executor configuration.
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        broker = GoogleInferenceCredential()
        first = asyncio.create_task(broker.credential())
        waiters = []
        try:
            assert await asyncio.to_thread(started.wait, 3)
            waiters = [asyncio.create_task(broker.credential()) for _ in range(12)]
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (
                await asyncio.wait_for(asyncio.to_thread(lambda: "database-work"), 1)
                == "database-work"
            )
            assert calls == ["refresh"]
        finally:
            release.set()
            await asyncio.gather(first, *waiters, return_exceptions=True)
        assert await broker.credential() == "google-access-token"
        assert calls == ["refresh"]

    asyncio.run(probe())


@pytest.mark.asyncio
async def test_shared_refresh_failure_reaches_waiters_and_can_be_retried(monkeypatch):
    import google.auth

    credentials = SimpleNamespace(valid=False, token=None)
    attempts = []

    def refresh(request):
        attempts.append("refresh")
        if len(attempts) == 1:
            raise ValueError("Refresh unavailable")
        credentials.token = "google-access-token"
        credentials.valid = True

    credentials.refresh = refresh
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (credentials, None))
    broker = GoogleInferenceCredential()
    results = await asyncio.gather(
        broker.credential(), broker.credential(), return_exceptions=True
    )
    assert all(isinstance(result, ValueError) for result in results)
    assert attempts == ["refresh"]
    assert await broker.credential() == "google-access-token"
    assert attempts == ["refresh", "refresh"]


@pytest.mark.parametrize("endpoint", ["chat/completions", "responses"])
def test_openrouter_claude_enables_automatic_caching(endpoint):
    upstream = InferenceUpstream.parse({"kind": "openrouter"})
    payload = {"model": "review", "messages": [{"role": "user", "content": "review"}]}
    _, body = upstream.request(endpoint, payload, "anthropic/claude-test")
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload


@pytest.mark.parametrize("setting", [None, {"type": "ephemeral", "ttl": "1h"}])
def test_openrouter_preserves_explicit_cache_settings(setting):
    upstream = InferenceUpstream.parse({"kind": "openrouter"})
    _, body = upstream.request(
        "chat/completions", {"cache_control": setting}, "anthropic/claude-test"
    )
    assert body["cache_control"] == setting


def test_openrouter_preserves_explicit_block_breakpoints():
    upstream = InferenceUpstream.parse({"kind": "openrouter"})
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "review", "cache_control": {"type": "ephemeral"}}
                ],
            }
        ]
    }
    _, body = upstream.request("chat/completions", payload, "anthropic/claude-test")
    assert "cache_control" not in body
    assert body["messages"] == payload["messages"]


@pytest.mark.parametrize(
    "kind,model", [("openrouter", "openai/test"), ("openai", "anthropic/claude-test")]
)
def test_other_upstreams_and_models_do_not_receive_cache_controls(kind, model):
    upstream = InferenceUpstream(kind, "https://inference.example/v1")
    _, body = upstream.request("chat/completions", {}, model)
    assert "cache_control" not in body
