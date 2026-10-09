"""Saved inference configuration reaches the native Pi model registry."""

import json

import pytest

from omnigent.harnesses.pi_native import credentials as creds
from omnigent.inference_config import inference_config_scope, snapshot_runtime_config


@pytest.mark.parametrize("selected", ["anthropic/claude-test-large", "openai/test-small"])
def test_saved_pi_model_limits_reach_native_launch(tmp_path, monkeypatch, selected):
    large, small = "anthropic/claude-test-large", "openai/test-small"
    monkeypatch.setenv("PI_LIMITS_TEST_KEY", "test-key")
    monkeypatch.setattr(creds, "_catalog_entry_for_model", lambda _: None)
    config = {
        "providers": {
            "gateway": {
                "kind": "gateway",
                "anthropic": {
                    "base_url": "https://gateway.example/anthropic",
                    "api_key_ref": "env:PI_LIMITS_TEST_KEY",
                    "context_window": 128000,
                    "max_output_tokens": 16384,
                },
                "openai": {
                    "base_url": "https://gateway.example/v1",
                    "api_key_ref": "env:PI_LIMITS_TEST_KEY",
                    "wire_api": "chat",
                },
            }
        },
        "inference": {
            "harnesses": {
                "pi-native": {
                    "provider": "gateway",
                    "default_model": large,
                    "model_allowlist": [large, small],
                    "model_limits": {
                        large: {"context_window": 1000000, "max_output_tokens": 128000},
                        small: {"context_window": 200000, "max_output_tokens": 32000},
                    },
                }
            }
        },
    }
    saved = json.loads(json.dumps({"runtime_config": config}))
    with inference_config_scope(snapshot_runtime_config(saved)):
        provider = creds.resolve_pi_native_provider(model=selected, config_loader=dict)
    assert provider is not None
    launch = creds.pi_native_provider_launch(tmp_path / "pi-agent", provider)
    rendered = json.loads((tmp_path / "pi-agent" / "models.json").read_text())
    entries = {
        row["id"]: row for group in rendered["providers"].values() for row in group["models"]
    }
    assert set(entries) == {large, small}
    assert entries[large]["contextWindow"] == 1000000
    assert entries[large]["maxTokens"] == 128000
    assert entries[small]["contextWindow"] == 200000
    assert entries[small]["maxTokens"] == 32000
    assert launch.env["PI_CODING_AGENT_DIR"] == str(tmp_path / "pi-agent")
    assert launch.args[launch.args.index("--model") + 1].endswith("/" + selected)
