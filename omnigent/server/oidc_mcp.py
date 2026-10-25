"""Owner-bound OIDC credentials for an operator-configured MCP upstream."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from omnigent.entities import ProviderConnection
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.oidc_machine_auth import _BoundedJWKClient, verify_access_token
from omnigent.stores.credential_store import CredentialStore

if TYPE_CHECKING:
    from omnigent.server.oidc_login import OIDCLoginBroker

_PROVIDER = "oidc_mcp"
_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:"


class DelegationUnavailable(Exception):
    """No usable owner credential; callers must never substitute another identity."""


class DelegationNotRenewable(DelegationUnavailable):
    """The caller's IdP session cannot yield an offline grant; the person must sign in again."""


@dataclass(frozen=True)
class MCPDelegationConfig:
    upstream_url: str
    audience: str
    scope: str
    machine_client_id: str
    machine_client_secret: str = field(repr=False)
    machine_scope: str | None = None

    @classmethod
    def parse(cls, value: object, *, section: str = "oidc_mcp") -> MCPDelegationConfig | None:
        """Load trusted operator configuration, including environment-expanded credentials."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"{section} must be a mapping")
        import os

        for key in (
            "upstream_url",
            "audience",
            "scope",
            "machine_client_id",
            "machine_client_secret_env",
        ):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"{section}.{key} must be a non-empty string")
        for key in ("machine_scope",):
            if value.get(key) is not None and (
                not isinstance(value[key], str) or not value[key].strip()
            ):
                raise ValueError(f"{section}.{key} must be a non-empty string")
        result = cls(
            machine_scope=value.get("machine_scope"),
            upstream_url=str(value.get("upstream_url", "")),
            audience=str(value.get("audience", "")),
            scope=str(value.get("scope", "")),
            machine_client_id=str(value.get("machine_client_id", "")),
            machine_client_secret=os.environ.get(
                str(value.get("machine_client_secret_env", "")), ""
            ),
        )
        parsed = urlsplit(result.upstream_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or not all(
                (
                    result.audience,
                    result.scope,
                    result.machine_client_id,
                    result.machine_client_secret,
                )
            )
        ):
            raise ValueError(
                f"{section} requires an HTTPS upstream, audience, scope "
                "and machine client credentials"
            )
        return result


class MCPDelegationBroker:
    """Exchange and refresh a verified owner's grant without exposing refresh tokens."""

    def __init__(
        self,
        config: MCPDelegationConfig,
        oidc: OIDCConfig,
        store: CredentialStore,
        auth: UnifiedAuthProvider,
        client: httpx.Client | None = None,
        *,
        provider: str = _PROVIDER,
    ) -> None:
        if oidc.provider_type != "oidc" or not oidc.jwks_uri or not oidc.client_secret:
            raise ValueError(f"{provider} requires a confidential OIDC login client with JWKS")
        self.config, self.oidc, self.store, self.auth = config, oidc, store, auth
        self.login_broker: OIDCLoginBroker | None = None
        self.provider = provider
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self.jwks = _BoundedJWKClient(oidc.jwks_uri)

    def _client_credentials(self, owner: str) -> dict[str, str]:
        if owner.startswith("oidc-machine:"):
            verifier = self.auth.machine_verifier
            if verifier is None or not verifier.principal_allowed(owner):
                raise DelegationUnavailable("Machine identity is no longer authorized")
            return {
                "client_id": self.config.machine_client_id,
                "client_secret": self.config.machine_client_secret,
            }
        return {"client_id": self.oidc.client_id, "client_secret": self.oidc.client_secret}

    def _tokens(self, owner: str, subject: str, form: dict[str, Any]) -> dict[str, Any]:
        credentials = self._client_credentials(owner)
        try:
            response = self.client.post(self.oidc.token_endpoint, data={**form, **credentials})
            response.raise_for_status()
            result = response.json()
            token = result["access_token"]
            claims = verify_access_token(token, self.jwks, self.oidc.issuer, self.config.audience)
            if (
                claims is None
                or claims.get("sub") != subject
                or claims.get("azp") != credentials["client_id"]
                or str(result.get("token_type", "")).lower() != "bearer"
            ):
                raise ValueError("Invalid delegated token identity")
            expiry = claims["exp"]
            if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
                raise ValueError("Invalid token expiry")
            expires_at = min(float(expiry), time.time() + float(result["expires_in"]))
            if expires_at <= time.time() + 10:
                raise ValueError("Delegated token expires too soon")
            return {
                "access_token": token,
                "refresh_token": result.get("refresh_token"),
                "expires_at": expires_at,
            }
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise DelegationUnavailable(
                "MCP identity unavailable; sign in or renew delegation"
            ) from None

    def capture(self, owner: str, subject: str, token: str) -> None:
        """Persist a grant only after the caller's IdP token and owner have been verified."""
        if self.login_broker is not None and not owner.startswith("oidc-machine:"):
            self._capture_login(owner, subject, token)
            return
        tokens = self._tokens(
            owner,
            subject,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token_type": _TOKEN_TYPE + "access_token",
                "subject_token": token,
                "requested_token_type": _TOKEN_TYPE + "refresh_token",
                # Scopes supply audiences and roles; audience filtering can strip role scopes.
                "scope": (self.config.machine_scope or self.config.scope)
                if owner.startswith("oidc-machine:")
                else self.config.scope,
            },
        )
        if not isinstance(tokens["refresh_token"], str) or not tokens["refresh_token"]:
            raise DelegationUnavailable("MCP delegation requires a renewable IdP session")
        self.store.upsert(
            owner,
            self.provider,
            secret=tokens,
            metadata={"issuer": self.oidc.issuer, "subject": subject},
        )

    def _capture_login(self, owner: str, subject: str, token: str) -> None:
        """Reuse the owner's offline login grant, creating it from the bearer when absent."""
        login = self.login_broker
        assert login is not None
        try:
            state = login.state(owner)
        except DelegationUnavailable:
            state = None
        if state is not None:
            if state["subject"] != subject:
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            self.credential(owner)
            return
        known = login.known_subject(owner)
        if known is not None and known != subject:
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        login.capture_exchanged(owner, subject, token)
        self.credential(owner)

    def credential(self, owner: str) -> str:
        """Resolve only the stored owner's grant and serialize refresh across replicas."""
        self._client_credentials(owner)
        if self.login_broker is not None and not owner.startswith("oidc-machine:"):
            state = self.login_broker.state(owner)
            # Only short-lived access tokens leave the shared offline login broker.
            current = self.store.get(owner, self.provider, with_secret=True)
            if (
                current is not None
                and current.secret is not None
                and current.metadata.get("generation") == state["generation"]
                and float(current.secret.get("expires_at", 0)) > time.time() + 30
            ):
                if not self.login_broker.active(owner, state["generation"]):
                    raise DelegationUnavailable("Sign in again to renew identity delegation")
                return str(current.secret["access_token"])
            tokens = self._tokens(
                owner,
                state["subject"],
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token_type": _TOKEN_TYPE + "access_token",
                    "subject_token": state["access_token"],
                    "requested_token_type": _TOKEN_TYPE + "access_token",
                    "scope": self.config.scope,
                },
            )
            tokens.pop("refresh_token", None)
            self.store.upsert(
                owner,
                self.provider,
                secret=tokens,
                metadata={
                    "issuer": self.oidc.issuer,
                    "subject": state["subject"],
                    "generation": state["generation"],
                },
            )
            if not self.login_broker.active(owner, state["generation"]):
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            return str(tokens["access_token"])

        def refresh(connection: ProviderConnection) -> dict[str, Any]:
            if "generation" in connection.metadata:
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            secret = connection.secret or {}
            if connection.metadata.get("issuer") != self.oidc.issuer:
                raise DelegationUnavailable("MCP identity requires a new sign-in")
            subject = connection.metadata.get("subject")
            if not isinstance(subject, str) or not subject:
                raise DelegationUnavailable("MCP identity requires a new sign-in")
            if owner.startswith("oidc-machine:"):
                verifier = self.auth.machine_verifier
                if verifier is None:
                    raise DelegationUnavailable("Machine identity is no longer authorized")
                expected = verifier.config.clients.get(owner.removeprefix("oidc-machine:"))
                if expected != subject:
                    raise DelegationUnavailable("Machine identity binding changed")
            if float(secret.get("expires_at", 0)) > time.time() + 30:
                return secret
            refresh_token = secret.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise DelegationUnavailable("MCP identity requires a new sign-in")
            tokens = self._tokens(
                owner, subject, {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
            tokens["refresh_token"] = tokens["refresh_token"] or refresh_token
            return tokens

        connection = self.store.refresh_locked(owner, self.provider, refresh)
        if connection is None or connection.secret is None:
            raise DelegationUnavailable("No MCP identity; sign in again or renew API delegation")
        self._client_credentials(owner)
        return str(connection.secret["access_token"])

    def capture_bearer(self, token: str) -> None:
        """Accept only independently verified IdP access tokens, never local session JWTs."""
        owner = None
        verifier = None
        for candidate in (self.auth.machine_verifier, self.auth.human_verifier):
            if candidate is not None:
                owner = candidate.authenticate(token)
                if owner is not None:
                    verifier = candidate
                    break
        if owner is None or verifier is None:
            return
        claims = verify_access_token(token, self.jwks, self.oidc.issuer, verifier.config.audience)
        if claims is None or not isinstance(claims.get("sub"), str):
            raise DelegationUnavailable("Invalid MCP delegation identity")
        subject = str(claims["sub"])
        current = self.store.get(owner, self.provider)
        if current is not None and current.metadata == {
            "issuer": self.oidc.issuer,
            "subject": claims["sub"],
        }:
            try:
                self.credential(owner)
                return
            except DelegationUnavailable:
                pass
        self.capture(owner, subject, token)
