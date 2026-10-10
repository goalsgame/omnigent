"""Delegated humans share browser identities without machine-token fallback."""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc_human_auth import OIDCHumanConfig, OIDCHumanVerifier
from tests.server.test_oidc_machine_auth import (
    SUBJECT,
    connection,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

__all__ = ["machine_config", "oidc", "signing_key", "verifier"]


@pytest.fixture()
def human(oidc, verifier):
    instance = OIDCHumanVerifier(
        OIDCHumanConfig("agent-api", "omnigent-access", frozenset({"connectors-exchange"})),
        oidc,
        frozenset({SUBJECT}),
    )
    instance.set_identity_check(lambda email: True)
    return instance


def human_token(key, **overrides):
    claims = {
        "azp": "connectors-exchange",
        "sub": "human-subject",
        "preferred_username": "person",
        "email": " Person@Example.Test ",
        "email_verified": True,
        "scope": "openid email omnigent-access",
    }
    claims.update(overrides)
    return signed_token(key, **claims)


def test_human_maps_to_browser_identity(human, signing_key, oidc, verifier):
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc, human_verifier=human)
    token = human_token(signing_key)
    assert provider.get_user_id(connection(token)) == "person@example.test"
    assert provider.get_user_id(connection(token, websocket=True)) == "person@example.test"
    assert provider.get_user_id(connection(token, "/v1/me")) is None
    assert provider.get_user_id(connection(token, cookie=True)) is None
    assert not provider._cookie_cache


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "connectors-api"},
        {"iss": "https://other.example.test"},
        {"azp": "other-client"},
        {"azp": ["connectors-exchange"]},
        {"sub": SUBJECT},
        {"sub": ""},
        {"preferred_username": "service-account-anything"},
        {"preferred_username": None},
        {"client_id": "machine-client"},
        {"clientId": "machine-client"},
        {"is_service_account": True},
        {"email": None},
        {"email": "oidc-machine:ticket-worker"},
        {"email_verified": False},
        {"email_verified": None},
        {"scope": "openid email"},
        {"scope": "omnigent-access-extra"},
        {"scope": ["omnigent-access"]},
        {"typ": "ID"},
    ],
)
def test_human_rejection(human, signing_key, claims):
    assert human.authenticate(human_token(signing_key, **claims)) is None


def test_admission_is_live_and_unwired_fails_closed(human, signing_key):
    allowed = [True]
    human.set_identity_check(lambda email: allowed[0])
    token = human_token(signing_key)
    assert human.authenticate(token) == "person@example.test"
    allowed[0] = False
    assert human.authenticate(token) is None
    human._identity_check = None
    assert human.authenticate(token) is None


def test_reserved_group_identity_is_rejected_before_account_callback(human, signing_key):
    check = Mock(side_effect=ValueError("Group principals cannot become users"))
    human.set_identity_check(check)
    assert human.authenticate(human_token(signing_key, email="oidc-group:ZW5naW5lZXJpbmc")) is None
    check.assert_not_called()


def test_client_dispatch_never_falls_back(human, verifier, oidc, signing_key, monkeypatch):
    provider = UnifiedAuthProvider(
        "oidc", oidc_config=oidc, human_verifier=human, machine_verifier=verifier
    )
    human_auth = Mock(return_value="person@example.test")
    monkeypatch.setattr(human, "authenticate", human_auth)
    assert provider.get_user_id(connection(signed_token(signing_key, sub="wrong-subject"))) is None
    human_auth.assert_not_called()
    machine_auth = Mock(return_value="oidc-machine:ticket-worker")
    monkeypatch.setattr(verifier, "authenticate", machine_auth)
    human_auth.return_value = None
    assert provider.get_user_id(connection(human_token(signing_key))) is None
    machine_auth.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"audience": "api", "scope": "two scopes", "clients": ["c"]},
        {"audience": "api", "scope": "sessions", "clients": []},
        {"audience": "api", "scope": "sessions", "clients": [1]},
    ],
)
def test_bad_human_config(value):
    with pytest.raises(RuntimeError):
        OIDCHumanConfig.parse(value)


def test_https_and_source_required(oidc):
    config = OIDCHumanConfig("api", "sessions", frozenset({"client"}))
    with pytest.raises(RuntimeError, match="HTTPS"):
        OIDCHumanVerifier(config, dataclasses.replace(oidc, jwks_uri="http://example.test/keys"))
    with pytest.raises(RuntimeError, match="oidc auth mode"):
        UnifiedAuthProvider("accounts", human_verifier=OIDCHumanVerifier(config, oidc))


def test_human_config_factory_and_overlap(oidc, tmp_path, monkeypatch):
    import json
    from unittest.mock import patch

    from omnigent.server.auth import create_auth_provider
    from omnigent.server.oidc import OIDCConfig

    path = tmp_path / "config.json"
    config = {
        "oidc_human_auth": {
            "audience": "agent-api",
            "scope": "omnigent-access",
            "clients": ["connectors-exchange"],
        }
    }
    path.write_text(json.dumps(config))
    monkeypatch.setenv("OMNIGENT_CONFIG", str(path))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "oidc")
    with patch.object(OIDCConfig, "from_env", return_value=oidc):
        provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    assert provider.human_verifier is not None
    assert provider.human_verifier.config.clients == frozenset({"connectors-exchange"})
    config["oidc_machine_auth"] = {
        "audience": "agent-api",
        "role_client": "web",
        "role": "automation",
        "clients": {"connectors-exchange": "machine-subject"},
    }
    path.write_text(json.dumps(config))
    with patch.object(OIDCConfig, "from_env", return_value=oidc):
        with pytest.raises(RuntimeError, match="must not overlap"):
            create_auth_provider()
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    with pytest.raises(RuntimeError, match="oidc auth mode"):
        create_auth_provider()


def test_human_rejections_do_not_log_email_claims(human, signing_key, caplog):
    token = human_token(signing_key, email_verified=False, email="private@example.test")
    assert human.authenticate(token) is None
    assert "private@example.test" not in caplog.text
    assert token not in caplog.text
