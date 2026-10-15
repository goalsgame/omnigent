"""Lore policy validation, separate grants and current machine bindings."""

from dataclasses import replace

import pytest

from omnigent.server.lore import LoreConfig, LoreDelegationBroker
from omnigent.server.oidc_mcp import DelegationUnavailable
from tests.server import test_oidc_mcp


@pytest.fixture
def broker(db_uri, monkeypatch):
    return test_oidc_mcp.broker.__wrapped__(db_uri, monkeypatch)


REMOTE = "lores://lore.example.test/game"


def config(base, machines=()):
    return LoreConfig(
        replace(
            base.config,
            upstream_url="https://lore.example.test",
            audience="lore",
            scope="openid lore-access",
        ),
        {REMOTE: None},
        tuple(machines),
    )


def test_separate_renewable_lore_grant(broker):
    base, _claims, requests = broker
    service = LoreDelegationBroker(config(base), base.oidc, base.store, base.auth, base.client)
    base.capture("alice@example.com", "person-sub", "original")
    service.capture("alice@example.com", "person-sub", "original")
    assert base.store.get("alice@example.com", "oidc_mcp") is not None
    assert base.store.get("alice@example.com", "oidc_lore") is not None
    assert requests[-1]["scope"] == ["openid lore-access"]
    current = base.store.get("alice@example.com", "oidc_lore", with_secret=True)
    base.store.update_secret(
        "alice@example.com", "oidc_lore", secret={**current.secret, "expires_at": 0}
    )
    assert service.credential("alice@example.com") == "access"
    assert requests[-1]["grant_type"] == ["refresh_token"]
    with pytest.raises(DelegationUnavailable):
        service.credential("bob@example.com")


def test_machine_requires_opt_in_and_live_subject(broker):
    base, claims, _requests = broker
    service = LoreDelegationBroker(config(base), base.oidc, base.store, base.auth, base.client)
    with pytest.raises(DelegationUnavailable, match="not configured"):
        service.capture("oidc-machine:bot", "bot-sub", "original")
    service.lore_config = config(base, ["oidc-machine:bot"])
    claims.update(sub="bot-sub", azp="machine-exchange")
    service.capture("oidc-machine:bot", "bot-sub", "original")
    assert service.credential("oidc-machine:bot") == "access"
    base.auth.machine_verifier.config.clients["bot"] = "replacement"
    with pytest.raises(DelegationUnavailable, match="binding changed"):
        service.credential("oidc-machine:bot")


@pytest.mark.parametrize(
    "remote",
    [
        "lore://lore.example.test/game",
        "lores://user:secret@lore.example.test/game",
        "lores://evil.example.test/game",
        "lores://lore.example.test/game?x=1",
        "lores://lore.example.test/../game",
        "lores://lore.example.test/game#main",
    ],
)
def test_config_rejects_unsafe_remote(monkeypatch, remote):
    monkeypatch.setenv("LORE_TEST_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError):
        LoreConfig.parse(
            {
                "upstream_url": "https://lore.example.test",
                "audience": "lore",
                "scope": "openid lore-access",
                "machine_client_id": "machine-exchange",
                "machine_client_secret_env": "LORE_TEST_CLIENT_SECRET",
                "repositories": {remote: {}},
            }
        )


def test_lore_refs_and_git_policy_remain_separate():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from omnigent.onboarding.sandboxes.types import GitCloneOptions
    from omnigent.server.managed_hosts import _apply_git_clone_options, parse_repo_workspace

    lore = parse_repo_workspace(REMOTE + "#main@369")
    git = parse_repo_workspace("https://git.example.test/team/repo#main")
    policy = GitCloneOptions(depth=1)
    launcher = SimpleNamespace(
        provider="agent_sandbox", capabilities=SimpleNamespace(git_clone_options=True)
    )
    repos = _apply_git_clone_options(launcher, [lore, git], policy)
    assert repos[0].git_clone == GitCloneOptions()
    assert repos[0].branch == "main@369"
    assert repos[1].git_clone == policy
    assert parse_repo_workspace(REMOTE + "#" + "a" * 64).branch == "a" * 64
    launcher.provider = "modal"
    with pytest.raises(HTTPException, match="Lore checkouts require"):
        _apply_git_clone_options(launcher, [lore], GitCloneOptions())
