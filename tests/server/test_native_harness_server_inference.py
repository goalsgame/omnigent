"""Server inference for the Claude Code and Codex native harnesses."""

from __future__ import annotations

import pytest

from omnigent.host.inference_proxy import claude_model_pins, prepare, runtime_config
from omnigent.server.inference_proxy import InferenceProxyConfig
from omnigent.server.inference_upstreams import InferenceUpstream

URL = "https://server.example/v1/inference/sessions/proxy-session"
CLAUDE = ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"]


def _config(
    monkeypatch: pytest.MonkeyPatch, harnesses: list[str], **extra
) -> InferenceProxyConfig:
    monkeypatch.setenv("TEST_SIGNING_KEY", "k" * 32)
    raw = {
        "public_url": "https://server.example",
        "signing_key_env": "TEST_SIGNING_KEY",
        "models": ["openai/gpt-test", *CLAUDE],
        "harnesses": harnesses,
        **extra,
    }
    config = InferenceProxyConfig.parse(raw)
    assert config is not None
    return config


def test_openrouter_serves_anthropic_messages_for_claude_models_only():
    upstream = InferenceUpstream.parse({"kind": "openrouter"})
    url, body = upstream.request("v1/messages", {"model": "x", "stream": True}, CLAUDE[0])
    assert url == "https://openrouter.ai/api/v1/messages"
    assert body == {"model": CLAUDE[0], "stream": True}
    assert "cache_control" not in body
    with pytest.raises(ValueError, match="Anthropic messages"):
        upstream.request("v1/messages", {}, "openai/gpt-test")


def test_proxy_accepts_native_harnesses_and_scopes_their_models(monkeypatch):
    config = _config(monkeypatch, ["pi-native", "claude-native", "codex-native"])
    assert config.model_apis("claude-native") == dict.fromkeys(CLAUDE, "anthropic-messages")
    assert set(config.model_apis("codex-native")) == {"openai/gpt-test", *CLAUDE}
    assert set(config.model_apis("codex-native").values()) == {"openai-completions"}


def test_proxy_rejects_unknown_harness(monkeypatch):
    with pytest.raises(ValueError, match="claude-native and codex-native"):
        _config(monkeypatch, ["kimi-native"])


def test_claude_native_requires_an_anthropic_capable_model(monkeypatch):
    monkeypatch.setenv("TEST_SIGNING_KEY", "k" * 32)
    with pytest.raises(ValueError, match="compatible model"):
        InferenceProxyConfig.parse(
            {
                "public_url": "https://server.example",
                "signing_key_env": "TEST_SIGNING_KEY",
                "models": ["openai/gpt-test"],
                "harnesses": ["claude-native"],
            }
        )


def test_claude_native_runtime_config_routes_claude_code_through_the_proxy(tmp_path):
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "claude-native",
        "models": CLAUDE,
        "model_apis": dict.fromkeys(CLAUDE, "anthropic-messages"),
    }
    config = prepare({"server_inference": proxy}, tmp_path / "grants")
    provider = config["providers"]["server-inference"]
    assert provider["kind"] == "gateway"
    assert provider["anthropic"]["base_url"] == URL
    assert provider["anthropic"]["auth_command"]
    assert provider["anthropic"]["models"] == {
        "default": CLAUDE[0],
        "opus": CLAUDE[0],
        "sonnet": CLAUDE[1],
        "haiku": CLAUDE[2],
    }
    harness = config["inference"]["harnesses"]["claude-native"]
    assert harness["provider"] == "server-inference"
    assert harness["model_allowlist"] == CLAUDE


def test_claude_native_rejects_openai_model_apis(tmp_path):
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "claude-native",
        "models": ["openai/gpt-test"],
        "model_apis": {"openai/gpt-test": "openai-completions"},
    }
    with pytest.raises(ValueError, match="model API"):
        prepare({"server_inference": proxy}, tmp_path / "grants")
    del proxy["model_apis"]
    with pytest.raises(ValueError, match="model API"):
        prepare({"server_inference": proxy}, tmp_path / "grants")


def test_codex_native_uses_the_responses_wire_api(tmp_path):
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "codex-native",
        "models": ["openai/gpt-test"],
    }
    config = runtime_config(proxy, tmp_path / "credential.json")
    assert config["providers"]["server-inference"]["openai"]["wire_api"] == "responses"
    assert "anthropic" not in config["providers"]["server-inference"]
    assert config["inference"]["harnesses"]["codex-native"]["model_allowlist"] == [
        "openai/gpt-test"
    ]


def test_openai_agents_keeps_the_chat_wire_api(tmp_path):
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "openai-agents",
        "models": ["openai/gpt-test"],
    }
    config = runtime_config(proxy, tmp_path / "credential.json")
    assert config["providers"]["server-inference"]["openai"]["wire_api"] == "chat"


def test_claude_pins_fall_back_when_a_tier_is_missing():
    assert claude_model_pins(["anthropic/claude-sonnet-5.5"]) == {
        "default": "anthropic/claude-sonnet-5.5",
        "sonnet": "anthropic/claude-sonnet-5.5",
        "haiku": "anthropic/claude-sonnet-5.5",
    }


def test_claude_binding_parses_and_keeps_alias_pins(tmp_path):
    from omnigent.inference_config import binding_for_harness, resolve_bound_provider

    sonnet = "anthropic/claude-sonnet-5.5"
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "claude-native",
        "models": [sonnet],
        "model_apis": {sonnet: "anthropic-messages"},
    }
    config = prepare({"server_inference": proxy}, tmp_path / "grants")
    assert "model_apis" not in config["inference"]["harnesses"]["claude-native"]
    assert binding_for_harness(config, "claude-native") is not None
    entry = resolve_bound_provider(config, "claude-native")
    assert entry is not None
    models = entry.family("anthropic").models
    assert models["sonnet"] == sonnet
    assert models["haiku"] == sonnet
    assert models["default"] == sonnet


def test_codex_excludes_chat_only_vertex_routes(monkeypatch):
    vertex = {"kind": "vertex-openai", "project": "example-project", "location": "global"}
    monkeypatch.setenv("TEST_SIGNING_KEY", "k" * 32)
    base = {
        "public_url": "https://server.example",
        "signing_key_env": "TEST_SIGNING_KEY",
        "models": ["google/gemini-test", "openai/gpt-test"],
        "upstreams": {"vertex": vertex, "router": {"kind": "openrouter"}},
        "model_routes": {
            "google/gemini-test": {"upstream": "vertex", "model": "gemini-test"},
            "openai/gpt-test": {"upstream": "router", "model": "openai/gpt-test"},
        },
    }
    config = InferenceProxyConfig.parse({**base, "harnesses": ["codex-native", "openai-agents"]})
    assert config is not None
    assert list(config.model_apis("codex-native")) == ["openai/gpt-test"]
    assert set(config.model_apis("openai-agents")) == {"google/gemini-test", "openai/gpt-test"}
    with pytest.raises(ValueError, match="compatible model"):
        InferenceProxyConfig.parse(
            {
                **base,
                "models": ["google/gemini-test"],
                "upstreams": {"vertex": vertex},
                "model_routes": {
                    "google/gemini-test": {"upstream": "vertex", "model": "gemini-test"}
                },
                "harnesses": ["codex-native"],
            }
        )


def test_claude_gateway_launch_rewrites_canonical_fallback_ids(tmp_path):
    from omnigent.harnesses.claude_native.main import _native_claude_config_from_entry
    from omnigent.inference_config import resolve_bound_provider

    opus = "anthropic/claude-opus-4.8"
    proxy = {
        "url": URL,
        "grant": "g" * 64,
        "harness": "claude-native",
        "models": [opus],
        "model_apis": {opus: "anthropic-messages"},
    }
    config = prepare({"server_inference": proxy}, tmp_path / "grants")
    entry = resolve_bound_provider(config, "claude-native")
    assert entry is not None
    launch = _native_claude_config_from_entry(entry, refresh_models=False)
    assert launch is not None
    assert launch.model_overrides["claude-opus-4-8"] == opus
    assert launch.model_overrides["claude-opus-4.8"] == opus
    assert set(launch.model_overrides.values()) == {opus}
