"""Native threshold policy must not inflate summary budgets or falsify model limits."""

import json

import pytest

from omnigent.inner import pi_settings
from omnigent.inner.pi_compaction import POLICY_FILE, TARGETS_ENV, economic_settings

KEY = "managed/vendor/large-model"
WINDOWS = {KEY: 1050000, "managed/vendor/small-model": 128000}


def test_opt_in_targets_do_not_modify_summary_budget_or_other_models():
    initial = {"theme": "dark", "compaction": {"keepRecentTokens": 20000}}
    settings, policy = economic_settings(initial, WINDOWS, '{"vendor/large-model":200000}')
    assert settings["compaction"]["modelOverrides"][KEY]["reserveTokens"] == 850000
    assert policy[KEY] == {
        "triggerReserveTokens": 850000,
        "summaryReserveTokens": 16384,
        "targetTokens": 200000,
    }
    assert initial == {"theme": "dark", "compaction": {"keepRecentTokens": 20000}}
    assert "managed/vendor/small-model" not in settings["compaction"]["modelOverrides"]
    assert WINDOWS[KEY] == 1050000


def test_disabled_and_existing_summary_preferences_preserved():
    initial = {"compaction": {"enabled": False, "reserveTokens": 30000, "keepRecentTokens": 10000}}
    settings, policy = economic_settings(initial, WINDOWS, '{"vendor/large-model":200000}')
    assert settings["compaction"]["enabled"] is False
    assert settings["compaction"]["keepRecentTokens"] == 10000
    assert policy[KEY]["summaryReserveTokens"] == 30000


def test_tighter_model_overrides_and_small_windows_unchanged():
    initial = {"compaction": {"modelOverrides": {KEY: {"reserveTokens": 900000}}}}
    settings, policy = economic_settings(
        initial, WINDOWS, '{"vendor/large-model":200000,"vendor/small-model":200000}'
    )
    assert settings == initial
    assert policy == {}


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        '{"x":true}',
        '{"x":1.2}',
        '{"x":-1}',
        '{"x":0}',
        "{bad",
        '{"vendor/large-model":20000}',
        '{"x":9007199254740992}',
    ],
)
def test_invalid_targets_rejected(raw):
    with pytest.raises(ValueError):
        economic_settings({}, WINDOWS, raw)


@pytest.mark.parametrize("per_model", [False, True])
def test_headroom_uses_resolved_small_summary_reserve(per_model):
    compaction = (
        {"modelOverrides": {KEY: {"reserveTokens": 1000}}}
        if per_model
        else {"reserveTokens": 1000}
    )
    settings, policy = economic_settings(
        {"compaction": compaction}, WINDOWS, '{"vendor/large-model":25000}'
    )
    assert settings["compaction"]["modelOverrides"][KEY]["reserveTokens"] == 1025000
    assert policy[KEY]["summaryReserveTokens"] == 1000


@pytest.mark.parametrize("per_model", [False, True])
def test_headroom_rejects_large_global_or_model_summary_reserve(per_model):
    compaction = (
        {"modelOverrides": {KEY: {"reserveTokens": 100000}}}
        if per_model
        else {"reserveTokens": 100000}
    )
    with pytest.raises(ValueError, match="leave room"):
        economic_settings({"compaction": compaction}, WINDOWS, '{"vendor/large-model":50000}')


def test_setting_precedence_and_stale_policy_removal(monkeypatch, tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    original = {
        "compaction": {
            "reserveTokens": 18000,
            "modelOverrides": {KEY: {"keepRecentTokens": 25000}},
        }
    }
    (global_dir / "settings.json").write_text(json.dumps(original))
    managed = tmp_path / "managed"
    monkeypatch.setenv(TARGETS_ENV, '{"vendor/large-model":200000}')
    pi_settings.prepare_managed_pi_agent_dir(
        managed, global_agent_dir=global_dir, model_context_windows=WINDOWS
    )
    stored = json.loads((managed / "settings.json").read_text())
    assert stored["compaction"]["modelOverrides"][KEY] == {
        "reserveTokens": 850000,
        "keepRecentTokens": 25000,
    }
    assert json.loads((managed / POLICY_FILE).read_text())[KEY]["summaryReserveTokens"] == 18000
    assert json.loads((global_dir / "settings.json").read_text()) == original
    monkeypatch.delenv(TARGETS_ENV)
    pi_settings.prepare_managed_pi_agent_dir(
        managed, global_agent_dir=global_dir, model_context_windows=WINDOWS
    )
    assert json.loads((managed / "settings.json").read_text()) == original
    assert json.loads((managed / POLICY_FILE).read_text()) == {}


def test_native_launch_translates_targets_without_editing_models(monkeypatch, tmp_path):
    from omnigent.harnesses.pi_native import credentials
    from omnigent.inner import pi_settings

    monkeypatch.setattr(pi_settings, "DEFAULT_PI_AGENT_DIR", tmp_path / "global")
    monkeypatch.setenv(TARGETS_ENV, '{"vendor/large-model":200000}')
    provider = credentials.PiProviderConfig(
        provider_id="managed",
        base_url="https://example.test",
        api="openai-completions",
        model="vendor/large-model",
        api_key="synthetic-key",
        auth_header=True,
    )
    rendered = {
        "providers": {
            "managed": {
                "api": "openai-completions",
                "baseUrl": "https://example.test",
                "apiKey": "synthetic-key",
                "models": [
                    {"id": "vendor/large-model", "contextWindow": 1050000, "maxTokens": 128000}
                ],
            }
        }
    }
    monkeypatch.setattr(credentials.PiProviderConfig, "to_models_config", lambda _: rendered)
    managed = tmp_path / "agent"
    credentials.pi_native_provider_launch(managed, provider)
    assert json.loads((managed / "models.json").read_text()) == rendered
    assert (
        json.loads((managed / "settings.json").read_text())["compaction"]["modelOverrides"][KEY][
            "reserveTokens"
        ]
        == 850000
    )
    assert json.loads((managed / POLICY_FILE).read_text())[KEY]["summaryReserveTokens"] == 16384


def test_host_forwards_nonsecret_compaction_targets(monkeypatch):
    from omnigent.host.connect import _build_runner_env

    raw = '{"vendor/large-model":200000}'
    env = _build_runner_env(
        {TARGETS_ENV: raw},
        server_url="http://example.test",
        runner_id="runner",
        binding_token="synthetic-binding",
        workspace="/tmp/workspace",
        parent_pid=1,
    )
    assert env[TARGETS_ENV] == raw


def test_provider_qualified_model_ids_each_get_an_override():
    settings, policy = economic_settings(
        {}, {KEY: 1050000, "other/vendor/large-model": 500000}, '{"vendor/large-model":200000}'
    )
    assert (
        settings["compaction"]["modelOverrides"]["other/vendor/large-model"]["reserveTokens"]
        == 300000
    )
    assert set(policy) == {KEY, "other/vendor/large-model"}
