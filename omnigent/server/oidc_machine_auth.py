"""Opt-in validation of service-account access tokens from the login issuer."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import jwt

if TYPE_CHECKING:
    from omnigent.server.oidc import OIDCConfig

MACHINE_PRINCIPAL_PREFIX = "oidc-machine:"
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


class _BoundedJWKClient(jwt.PyJWKClient):
    """Unknown key IDs cannot force an unbounded sequence of issuer requests."""

    def __init__(self, uri: str) -> None:
        super().__init__(uri, timeout=5, lifespan=300)
        self._fetch_lock = threading.Lock()
        self._next_fetch = 0.0

    def fetch_data(self) -> object:
        with self._fetch_lock:
            now = time.monotonic()
            if now < self._next_fetch:
                raise jwt.PyJWKClientError("JWKS refresh is temporarily rate limited")
            self._next_fetch = now + 30
            return super().fetch_data()


@dataclass(frozen=True)
class OIDCMachineConfig:
    """Public audience, role and exact client/subject bindings; no client secrets."""

    audience: str
    role_client: str
    role: str
    clients: Mapping[str, str]

    @classmethod
    def parse(cls, value: object) -> OIDCMachineConfig | None:
        """Validate a configured section; absent configuration leaves auth disabled."""
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {
            "audience",
            "role_client",
            "role",
            "clients",
        }:
            raise RuntimeError(
                "oidc_machine_auth requires audience, role_client, role and clients"
            )
        strings = [value[key] for key in ("audience", "role_client", "role")]
        if any(not isinstance(item, str) or not item.strip() for item in strings):
            raise RuntimeError(
                "oidc_machine_auth audience and role fields must be non-empty strings"
            )
        clients = value["clients"]
        if not isinstance(clients, dict) or not clients:
            raise RuntimeError("oidc_machine_auth clients must bind client IDs to exact subjects")
        bindings: dict[str, str] = {}
        for client, subject in clients.items():
            if (
                not isinstance(client, str)
                or _CLIENT_ID.fullmatch(client) is None
                or not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 256
            ):
                raise RuntimeError("oidc_machine_auth contains an invalid client/subject binding")
            bindings[client] = subject
        return cls(*strings, clients=MappingProxyType(bindings))

    @property
    def principals(self) -> frozenset[str]:
        """Separate machine identities from human email identities."""
        return frozenset(MACHINE_PRINCIPAL_PREFIX + client for client in self.clients)


class OIDCMachineVerifier:
    """Validate RS256 bearer tokens; permission checks remain uncached per request."""

    def __init__(self, config: OIDCMachineConfig, oidc: OIDCConfig) -> None:
        if oidc.provider_type != "oidc" or not oidc.jwks_uri:
            raise RuntimeError("oidc_machine_auth requires an OIDC login issuer with JWKS")
        for url in (oidc.issuer, oidc.jwks_uri):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.fragment
            ):
                raise RuntimeError("oidc_machine_auth requires HTTPS issuer and JWKS URLs")
        self.config = config
        self._issuer = oidc.issuer
        self._jwks = _BoundedJWKClient(oidc.jwks_uri)
        self._principal_allowed: Callable[[str], bool] | None = None

    def set_principal_check(self, check: Callable[[str], bool]) -> None:
        """Install the application's live non-admin check before accepting tokens."""
        self._principal_allowed = check
        if any(not check(principal) for principal in self.config.principals):
            raise RuntimeError("OIDC machine principals must not be administrators")

    def principal_allowed(self, principal: str) -> bool:
        """Fail closed until wired, on removed bindings, and on admin promotion."""
        return (
            principal in self.config.principals
            and self._principal_allowed is not None
            and self._principal_allowed(principal)
        )

    def authenticate(self, token: str) -> str | None:
        """Verify signature and claims using only the operator-configured JWKS URL."""
        if len(token) > 16_384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 0 < len(kid) <= 256:
                return None
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self.config.audience,
                options={"require": ["iss", "aud", "sub", "exp", "iat", "azp"]},
            )
        except (jwt.PyJWTError, OSError, ValueError):
            return None
        client = claims.get("azp")
        if not isinstance(client, str) or self.config.clients.get(client) != claims.get("sub"):
            return None
        if claims.get("typ") != "Bearer":
            return None
        issued, expires = claims["iat"], claims["exp"]
        if type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 3600:
            return None
        access = claims.get("resource_access")
        if not isinstance(access, dict):
            return None
        resource = access.get(self.config.role_client)
        roles = resource.get("roles") if isinstance(resource, dict) else None
        if not isinstance(roles, list) or self.config.role not in roles:
            return None
        principal = MACHINE_PRINCIPAL_PREFIX + client
        return principal if self.principal_allowed(principal) else None
