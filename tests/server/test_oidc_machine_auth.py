"""Real RSA token verification, strict machine bindings and request confinement."""

from __future__ import annotations

import dataclasses
import json
import time
from io import BytesIO
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import HTTPConnection

from omnigent.server.auth import UnifiedAuthProvider, create_auth_provider
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.oidc_machine_auth import (
    OIDCMachineConfig,
    OIDCMachineVerifier,
    _BoundedJWKClient,
)

ISSUER = "https://identity.example.test/realms/example"
CLIENT = "ticket-worker"
SUBJECT = "service-account-subject"
PRINCIPAL = "oidc-machine:ticket-worker"


@pytest.fixture()
def oidc() -> OIDCConfig:
    return OIDCConfig(
        issuer=ISSUER,
        client_id="web",
        client_secret="unused",
        redirect_uri="https://app.example.test/auth/callback",
        cookie_secret=b"c" * 32,
        scopes="openid email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint=ISSUER + "/authorize",
        token_endpoint=ISSUER + "/token",
        jwks_uri=ISSUER + "/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )


@pytest.fixture()
def machine_config() -> OIDCMachineConfig:
    return OIDCMachineConfig("agent-api", "web", "automation", {CLIENT: SUBJECT})


@pytest.fixture()
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_token(signing_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "agent-api",
        "sub": SUBJECT,
        "azp": CLIENT,
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
        "resource_access": {"web": {"roles": ["automation"]}},
    }
    claims.update(overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "key-one"})


@pytest.fixture()
def verifier(oidc, machine_config, signing_key, monkeypatch) -> OIDCMachineVerifier:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update(kid="key-one", alg="RS256", use="sig")
    monkeypatch.setattr(
        "jwt.jwks_client.urllib.request.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps({"keys": [jwk]}).encode()),
    )
    instance = OIDCMachineVerifier(machine_config, oidc)
    instance.set_principal_check(lambda principal: True)
    return instance


def connection(
    token: str, path: str = "/v1/sessions", *, cookie: bool = False, websocket: bool = False
):
    header = (
        (b"cookie", f"__Host-ap_session={token}".encode())
        if cookie
        else (b"authorization", f"Bearer {token}".encode())
    )
    return HTTPConnection(
        {
            "type": "websocket" if websocket else "http",
            "path": path,
            "headers": [header],
            "scheme": "https",
            "server": ("app.example.test", 443),
            "query_string": b"",
        }
    )


def test_valid_service_account_token(verifier, signing_key):
    assert verifier.authenticate(signed_token(signing_key)) == PRINCIPAL


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://other.example.test"},
        {"aud": "other-api"},
        {"azp": "other-client"},
        {"sub": "human-subject"},
        {"typ": "ID"},
        {"resource_access": {}},
        {"resource_access": {"web": {"roles": "automation"}}},
        {"resource_access": {"web": {"roles": ["other-role"]}}},
        {"iat": int(time.time()) + 100, "exp": int(time.time()) + 400},
        {"iat": int(time.time()) - 1000, "exp": int(time.time()) - 1},
        {"iat": int(time.time()) - 100, "exp": int(time.time()) + 3601},
        {"iat": "invalid"},
        {"exp": "invalid"},
    ],
)
def test_reject_wrong_identity_or_claims(verifier, signing_key, overrides):
    assert verifier.authenticate(signed_token(signing_key, **overrides)) is None


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat", "azp"])
def test_required_claims(verifier, signing_key, claim):
    claims = jwt.decode(signed_token(signing_key), options={"verify_signature": False})
    del claims[claim]
    token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "key-one"})
    assert verifier.authenticate(token) is None


def test_wrong_signature_and_algorithm(verifier):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verifier.authenticate(signed_token(other)) is None
    assert verifier.authenticate(jwt.encode({"sub": SUBJECT}, "secret", algorithm="HS256")) is None
    assert verifier.authenticate("invalid") is None
    assert verifier.authenticate("x" * 16385) is None


def test_admin_promotion_revokes_token_without_cache(verifier, signing_key, oidc):
    admins = set()
    verifier.set_principal_check(lambda principal: principal not in admins)
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier)
    token = signed_token(signing_key)
    owner_token = provider.mint_runner_token(PRINCIPAL, 300)
    assert owner_token is not None
    assert provider.get_user_id(connection(token)) == PRINCIPAL
    assert provider.get_user_id(connection(owner_token)) == PRINCIPAL
    admins.add(PRINCIPAL)
    assert provider.get_user_id(connection(token)) is None
    assert provider.get_user_id(connection(owner_token)) is None


def test_unwired_or_admin_principal_fails_closed(oidc, machine_config, signing_key, monkeypatch):
    verifier = OIDCMachineVerifier(machine_config, oidc)
    assert not verifier.principal_allowed(PRINCIPAL)
    with pytest.raises(RuntimeError, match="administrators"):
        verifier.set_principal_check(lambda principal: False)


def test_machine_path_and_cookie_confinement(verifier, signing_key, oidc):
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier)
    token = signed_token(signing_key)
    assert provider.get_user_id(connection(token)) == PRINCIPAL
    assert (
        provider.get_user_id(connection(token, "/v1/sessions/s1/stream", websocket=True))
        == PRINCIPAL
    )
    assert provider.get_user_id(connection(token, "/v1/me")) is None
    assert provider.get_user_id(connection(token, "/v1/sessionsX")) is None
    assert provider.get_user_id(connection(token, "/auth/users")) is None
    assert provider.get_user_id(connection(token, cookie=True)) is None
    human = mint_session_token("person@example.test", oidc.cookie_secret, 300, "oidc")
    assert provider.get_user_id(connection(human, "/v1/me")) == "person@example.test"
    assert provider.get_user_id(connection(human, cookie=True)) == "person@example.test"
    disabled = UnifiedAuthProvider("oidc", oidc_config=oidc)
    assert disabled.get_user_id(connection(token)) is None


def test_multiple_audiences_and_no_human_impersonation(verifier, signing_key):
    token = signed_token(signing_key, aud=["account", "agent-api"], email="admin@example.test")
    assert verifier.authenticate(token) == PRINCIPAL


def test_issuer_outage_fails_closed(verifier, signing_key, monkeypatch):
    def unavailable(token):
        raise jwt.PyJWKClientError("unavailable")

    monkeypatch.setattr(verifier._jwks, "get_signing_key_from_jwt", unavailable)
    assert verifier.authenticate(signed_token(signing_key)) is None


def test_jwks_refresh_is_bounded_and_recovers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("omnigent.server.oidc_machine_auth.time.monotonic", lambda: now[0])
    calls = []
    monkeypatch.setattr(
        jwt.PyJWKClient, "fetch_data", lambda self: calls.append(1) or {"keys": []}
    )
    client = _BoundedJWKClient(ISSUER + "/certs")
    assert client.fetch_data() == {"keys": []}
    with pytest.raises(jwt.PyJWKClientError, match="rate limited"):
        client.fetch_data()
    now[0] += 31
    assert client.fetch_data() == {"keys": []}
    assert calls == [1, 1]


def test_config_factory_reads_yaml(oidc, tmp_path, monkeypatch):
    config = {
        "oidc_machine_auth": {
            "audience": "agent-api",
            "role_client": "web",
            "role": "automation",
            "clients": {CLIENT: SUBJECT},
        }
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("OMNIGENT_CONFIG", str(path))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "oidc")
    with patch.object(OIDCConfig, "from_env", return_value=oidc):
        provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    assert provider.machine_verifier is not None
    assert provider.machine_verifier.config.clients == {CLIENT: SUBJECT}
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    with pytest.raises(RuntimeError, match="oidc auth mode"):
        create_auth_provider()


@pytest.mark.parametrize(
    "value",
    [
        {},
        "invalid",
        {"audience": "api"},
        {"audience": "api", "role_client": "web", "role": "automation", "clients": {}},
        {
            "audience": "api",
            "role_client": "web",
            "role": "automation",
            "clients": {"person@example.test": SUBJECT},
        },
    ],
)
def test_invalid_config_rejected(value):
    with pytest.raises(RuntimeError):
        OIDCMachineConfig.parse(value)


def test_non_oidc_provider_rejected(oidc, machine_config):
    with pytest.raises(RuntimeError, match="JWKS"):
        OIDCMachineVerifier(machine_config, dataclasses.replace(oidc, jwks_uri=None))
    assert OIDCMachineConfig.parse(None) is None


def test_key_rotation_refreshes_and_preserves_cached_keys(verifier, signing_key, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("omnigent.server.oidc_machine_auth.time.monotonic", lambda: now[0])
    assert verifier.authenticate(signed_token(signing_key)) == PRINCIPAL
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    old_jwk.update(kid="key-one", alg="RS256", use="sig")
    new_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rotated.public_key()))
    new_jwk.update(kid="key-two", alg="RS256", use="sig")
    monkeypatch.setattr(
        "jwt.jwks_client.urllib.request.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps({"keys": [old_jwk, new_jwk]}).encode()),
    )
    claims = jwt.decode(signed_token(signing_key), options={"verify_signature": False})
    rotated_token = jwt.encode(claims, rotated, algorithm="RS256", headers={"kid": "key-two"})
    assert verifier.authenticate(rotated_token) is None
    assert verifier.authenticate(signed_token(signing_key)) == PRINCIPAL
    now[0] += 31
    assert verifier.authenticate(rotated_token) == PRINCIPAL
    assert verifier.authenticate(signed_token(signing_key)) == PRINCIPAL


def test_removed_client_binding_revokes_owner_token(verifier, oidc):
    provider = UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier)
    token = provider.mint_runner_token(PRINCIPAL, 300)
    assert token is not None
    assert provider.get_user_id(connection(token)) == PRINCIPAL
    verifier.config = dataclasses.replace(
        verifier.config, clients={"other-worker": "other-subject"}
    )
    assert provider.get_user_id(connection(token)) is None


def test_non_https_issuer_rejected(oidc, machine_config):
    with pytest.raises(RuntimeError, match="HTTPS"):
        OIDCMachineVerifier(
            machine_config,
            dataclasses.replace(oidc, jwks_uri="http://identity.example.test/certs"),
        )
