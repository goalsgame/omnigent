"""Go proxy configuration and owner-bound token exchange."""

import pytest

from omnigent.server.goproxy import GoProxyBroker, GoProxyConfig
from omnigent.server.oidc_mcp import DelegationUnavailable
from tests.server import test_oidc_mcp


@pytest.fixture
def broker(db_uri, monkeypatch):
    return test_oidc_mcp.broker.__wrapped__(db_uri, monkeypatch)


def parse(**overrides):
    value = {
        "url": "https://go.example.test",
        "audience": "goproxy",
        "scope": "openid goproxy",
        **overrides,
    }
    return GoProxyConfig.parse({k: v for k, v in value.items() if v is not None})


def test_absent_configuration_leaves_provider_off():
    assert GoProxyConfig.parse(None) is None


def test_prefix_defaults_to_the_proxy_host():
    config = parse(url="https://go.example.test/")
    assert (config.url, config.prefix) == ("https://go.example.test", "go.example.test")
    assert parse(prefix="/example.test/sdk/").prefix == "example.test/sdk"


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "http://go.example.test"},
        {"url": "https://user:pw@go.example.test"},
        {"url": "https://go.example.test/path"},
        {"url": "https://go.example.test?x=1"},
        {"url": "https://go.example.test#f"},
        {"url": None},
        {"audience": None},
        {"scope": ""},
        {"prefix": "https://go.example.test"},
        {"prefix": "go.example.test sdk"},
        {"extra": "x"},
    ],
)
def test_config_rejects_unsafe_values(overrides):
    with pytest.raises(ValueError):
        parse(**overrides)


def test_config_must_be_a_mapping():
    with pytest.raises(ValueError):
        GoProxyConfig.parse("https://go.example.test")


def test_matches_only_the_configured_url():
    config = parse()
    assert config.matches("https://go.example.test")
    assert config.matches("https://go.example.test/")
    for url in (None, "", "https://go.example.test/x", "http://go.example.test", "https://x.test"):
        assert not config.matches(url)


def login_state(base, generation="gen-1"):
    from types import SimpleNamespace

    base.login_broker = SimpleNamespace(
        state=lambda owner, generation=None: {
            "subject": "person-sub",
            "generation": generation,
            "access_token": "login-token",
        },
        active=lambda owner, generation: True,
    )


def test_person_token_is_exchanged_for_the_proxy_audience(broker):
    base, _claims, requests = broker
    service = GoProxyBroker(parse(), base.oidc, base.store, base.auth, base.client)
    login_state(service)
    assert service.credential("alice@example.com") == "access"
    assert requests[-1]["scope"] == ["openid goproxy"]
    assert requests[-1]["client_id"] == ["app"]
    assert requests[-1]["subject_token"] == ["login-token"]
    assert "audience" not in requests[-1]
    assert service.store.get("alice@example.com", "oidc_goproxy") is not None
    assert service.store.get("alice@example.com", "oidc_mcp") is None
    assert service.credential("alice@example.com") == "access"
    assert len(requests) == 1


@pytest.mark.parametrize("field,value", [("azp", "other-client"), ("sub", "someone-else")])
def test_exchange_rejects_wrong_identity(broker, field, value):
    base, claims, _ = broker
    service = GoProxyBroker(parse(), base.oidc, base.store, base.auth, base.client)
    login_state(service)
    claims[field] = value
    with pytest.raises(DelegationUnavailable):
        service.credential("alice@example.com")


def test_machine_principals_never_get_a_token(broker):
    base, _claims, requests = broker
    service = GoProxyBroker(parse(), base.oidc, base.store, base.auth, base.client)
    login_state(service)
    with pytest.raises(DelegationUnavailable, match="signed-in people"):
        service.credential("oidc-machine:bot")
    assert requests == []


def test_no_login_state_means_no_token(broker):
    base, _claims, requests = broker
    service = GoProxyBroker(parse(), base.oidc, base.store, base.auth, base.client)
    with pytest.raises(DelegationUnavailable):
        service.credential("alice@example.com")
    assert requests == []
