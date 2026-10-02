"""Opt-in delegated human access tokens from the existing login issuer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import jwt

from omnigent.server.oidc_machine_auth import (
    _CLIENT_ID,
    MACHINE_PRINCIPAL_PREFIX,
    _BoundedJWKClient,
    verify_access_token,
)

if TYPE_CHECKING:
    from omnigent.server.oidc import OIDCConfig
    from omnigent.server.oidc_machine_auth import OIDCMachineVerifier


@dataclass(frozen=True)
class OIDCHumanConfig:
    """Audience, required scope and allowlisted human-only OAuth clients."""

    audience: str
    scope: str
    clients: frozenset[str]

    @classmethod
    def parse(cls, value: object) -> OIDCHumanConfig | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"audience", "scope", "clients"}:
            raise RuntimeError("oidc_human_auth requires audience, scope and clients")
        audience, scope, clients = value["audience"], value["scope"], value["clients"]
        if (
            not isinstance(audience, str)
            or not audience.strip()
            or not isinstance(scope, str)
            or not scope
            or any(c.isspace() for c in scope)
            or not isinstance(clients, list)
            or not clients
            or any(not isinstance(c, str) or _CLIENT_ID.fullmatch(c) is None for c in clients)
        ):
            raise RuntimeError("oidc_human_auth has invalid audience, scope or clients")
        return cls(audience, scope, frozenset(clients))


class OIDCHumanVerifier:
    """Validate human-only client tokens and apply the browser's admission policy."""

    def __init__(
        self,
        config: OIDCHumanConfig,
        oidc: OIDCConfig,
        machine_subjects: frozenset[str] = frozenset(),
    ) -> None:
        if oidc.provider_type != "oidc" or not oidc.jwks_uri:
            raise RuntimeError("oidc_human_auth requires an OIDC login issuer with JWKS")
        for url in (oidc.issuer, oidc.jwks_uri):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.fragment
            ):
                raise RuntimeError("oidc_human_auth requires HTTPS issuer and JWKS URLs")
        self._machine_subjects = machine_subjects
        self.config = config
        self._oidc = oidc
        self._jwks = _BoundedJWKClient(oidc.jwks_uri)
        self._identity_check: Callable[[str], bool] | None = None

    def set_identity_check(self, check: Callable[[str], bool]) -> None:
        self._identity_check = check

    def authenticate(self, token: str) -> str | None:
        from omnigent.server.auth import _RESERVED_USERS
        from omnigent.server.routes.auth import resolve_verified_oidc_email

        claims = verify_access_token(token, self._jwks, self._oidc.issuer, self.config.audience)
        if claims is None:
            return None
        client = claims.get("azp")
        if not isinstance(client, str) or client not in self.config.clients:
            return None
        if (
            claims["sub"] in self._machine_subjects
            or "client_id" in claims
            or "clientId" in claims
            or claims.get("is_service_account") not in (None, False)
        ):
            return None
        scope = claims.get("scope")
        if not isinstance(scope, str) or self.config.scope not in scope.split():
            return None
        username = claims.get("preferred_username")
        if (
            not isinstance(username, str)
            or not username
            or username.startswith("service-account-")
        ):
            return None
        email = resolve_verified_oidc_email(claims, self._oidc, log_details=False)
        if email is None:
            return None
        email = email.lower()
        if email in _RESERVED_USERS or email.startswith(MACHINE_PRINCIPAL_PREFIX):
            return None
        if self._identity_check is None or not self._identity_check(email):
            return None
        return email


def authenticate_oidc_bearer(
    token: str, machine: OIDCMachineVerifier | None, human: OIDCHumanVerifier | None
) -> str | None:
    """Dispatch by client without fallback; each branch verifies the entire signed token."""
    if len(token) > 16_384:
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    client = claims.get("azp")
    if not isinstance(client, str):
        return None
    if machine is not None and client in machine.config.clients:
        return machine.authenticate(token)
    if human is not None and client in human.config.clients:
        return human.authenticate(token)
    return None
