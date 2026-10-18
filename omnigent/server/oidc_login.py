"""Bounded, server-held offline delegation for interactive OIDC logins."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from typing import Any

import httpx
import jwt

from omnigent.db.group_authority import group_name, verified_groups
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.oidc_machine_auth import _BoundedJWKClient
from omnigent.server.oidc_mcp import DelegationUnavailable
from omnigent.stores.credential_store import CredentialStore

PROVIDER = "oidc_login"


class OIDCLoginBroker:
    """Serialize offline-token rotation and cap every login at an absolute deadline."""

    def __init__(self, config: OIDCConfig, store: CredentialStore, ttl_hours: int) -> None:
        if config.provider_type != "oidc" or not config.jwks_uri or not config.client_secret:
            raise ValueError("Offline delegation requires confidential OIDC with JWKS")
        if "offline_access" not in config.scopes.split() or not 1 <= ttl_hours <= 720:
            raise ValueError(
                "Offline delegation requires offline_access and a 1–720 hour lifetime"
            )
        self.config, self.store = config, store
        self.ttl = ttl_hours * 3600
        self.jwks = _BoundedJWKClient(config.jwks_uri)
        self.client = httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self.cookie_key = hmac.new(
            config.cookie_secret, b"oidc-browser-renewal-v1", hashlib.sha256
        ).digest()
        self.cookie_name = "__Host-ap_oidc_renewal" if config.secure_cookies else "ap_oidc_renewal"

    def _claims(self, token: str, subject: str, *, offline: bool = False) -> dict[str, Any]:
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=self.config.issuer,
            options={
                "verify_aud": False,
                "verify_exp": not offline,
                "require": ["iss", "sub", "iat"],
            },
        )
        if claims.get("sub") != subject or claims.get("azp") != self.config.client_id:
            raise ValueError("OIDC delegation identity changed")
        if offline and claims.get("typ") != "Offline":
            raise ValueError("An offline refresh token is required")
        if not offline and claims.get("typ") != "Bearer":
            raise ValueError("An access token is required")
        return claims

    def capture(self, owner: str, subject: str, tokens: dict[str, Any]) -> dict[str, Any]:
        """Persist only a verified offline grant returned by the login code exchange."""
        try:
            refresh = tokens["refresh_token"]
            offline = self._claims(refresh, subject, offline=True)
            claims = self._claims(tokens["access_token"], subject)
            deadline = time.time() + self.ttl
            exp = offline.get("exp", 0)
            if isinstance(exp, (int, float)) and exp > 0:
                deadline = min(deadline, exp)
            if deadline <= time.time():
                raise ValueError("Offline grant expired")
            secret = self._secret(tokens, claims, refresh)
            self.store.upsert(
                owner,
                PROVIDER,
                secret=secret,
                metadata={
                    "issuer": self.config.issuer,
                    "subject": subject,
                    "generation": secrets.token_hex(24),
                    "deadline": deadline,
                },
            )
            return self.state(owner)
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None

    def _secret(
        self, tokens: dict[str, Any], claims: dict[str, Any], refresh: str
    ) -> dict[str, Any]:
        expiry = min(float(claims["exp"]), time.time() + float(tokens["expires_in"]))
        if not math.isfinite(expiry) or expiry <= time.time() + 10:
            raise ValueError("OIDC access token expires too soon")
        return {
            "access_token": tokens["access_token"],
            "refresh_token": refresh,
            "expires_at": expiry,
            "groups": [group_name(g) for g in verified_groups(claims.get("groups"))],
        }

    def active(self, owner: str, generation: str) -> bool:
        connection = self.store.get(owner, PROVIDER)
        return bool(
            connection
            and connection.metadata.get("issuer") == self.config.issuer
            and connection.metadata.get("generation") == generation
            and float(connection.metadata.get("deadline", 0)) > time.time()
        )

    def state(self, owner: str) -> dict[str, Any]:
        """Refresh one shared login grant, never a copy per downstream service."""

        def refresh(connection):
            meta, secret = connection.metadata, connection.secret or {}
            if (
                meta.get("issuer") != self.config.issuer
                or float(meta.get("deadline", 0)) <= time.time()
            ):
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            if float(secret.get("expires_at", 0)) > time.time() + 60:
                return secret
            try:
                response = self.client.post(
                    self.config.token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": secret["refresh_token"],
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                    },
                )
                response.raise_for_status()
                tokens = response.json()
                claims = self._claims(tokens["access_token"], str(meta["subject"]))
                if claims.get(self.config.email_claim, owner).lower() != owner:
                    raise ValueError("OIDC owner changed")
                rotated = tokens.get("refresh_token") or secret["refresh_token"]
                self._claims(rotated, str(meta["subject"]), offline=True)
                return self._secret(tokens, claims, rotated)
            except (
                httpx.HTTPError,
                jwt.PyJWTError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
            ):
                raise DelegationUnavailable("Sign in again to renew identity delegation") from None

        connection = self.store.refresh_locked(owner, PROVIDER, refresh)
        if connection is None or connection.secret is None:
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        return {**connection.secret, **connection.metadata}

    def group_authority(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "groups": state.get("groups", []),
            "expires_at": int(min(state["deadline"], state["expires_at"])),
        }

    def session_token(self, owner: str, state: dict[str, Any]) -> str:
        return mint_session_token(
            owner,
            self.config.cookie_secret,
            self.access_ttl(state),
            self.config.provider_type,
            group_authority=self.group_authority(state),
            oidc_grant_id=state["generation"],
        )

    def access_ttl(self, state: dict[str, Any]) -> int:
        return max(
            1, int(min(900, state["deadline"] - time.time(), state["expires_at"] - time.time()))
        )

    def browser_cookie(self, owner: str, state: dict[str, Any]) -> str:
        return jwt.encode(
            {"sub": owner, "generation": state["generation"], "exp": int(state["deadline"])},
            self.cookie_key,
            algorithm="HS256",
        )

    def browser_identity(self, token: str) -> tuple[str, str]:
        try:
            claims = jwt.decode(
                token,
                self.cookie_key,
                algorithms=["HS256"],
                options={"require": ["sub", "generation", "exp"]},
            )
            owner, generation = claims["sub"], claims["generation"]
            if (
                not isinstance(owner, str)
                or not isinstance(generation, str)
                or not self.active(owner, generation)
            ):
                raise ValueError("Login revoked")
            return owner, generation
        except (jwt.PyJWTError, ValueError, TypeError):
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None
