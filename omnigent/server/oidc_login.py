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
from omnigent.server.oidc_mcp import DelegationNotRenewable, DelegationUnavailable
from omnigent.stores.credential_store import CredentialStore

PROVIDER = "oidc_login"


class DelegationTemporarilyUnavailable(Exception):
    """The identity provider is unavailable; retain credentials for retry."""


class _LoginRefreshRejected(DelegationUnavailable):
    """The provider permanently rejected this login's refresh credential."""


class OIDCLoginRevoker:
    """Keep logout available independently of offline renewal configuration."""

    def __init__(self, config: OIDCConfig, store: CredentialStore) -> None:
        self.config, self.store = config, store
        self.cookie_key = hmac.new(
            config.cookie_secret, b"oidc-browser-renewal-v1", hashlib.sha256
        ).digest()
        self.cookie_name = "__Host-ap_oidc_renewal" if config.secure_cookies else "ap_oidc_renewal"

    def _connection(self, owner: str, generation: str, *, require_current_identity: bool = True):
        if not isinstance(generation, str) or not generation or generation == "current":
            return None
        connection = self.store.get(owner, PROVIDER, account_id=generation)
        if connection is None:
            # Existing installations kept their sole grant in the default slot.
            connection = self.store.get(owner, PROVIDER)
        if connection is None or connection.metadata.get("generation") != generation:
            return None
        current = self.store.get(owner, PROVIDER, account_id="current")
        if (
            require_current_identity
            and current is not None
            and (
                current.metadata.get("issuer") != connection.metadata.get("issuer")
                or current.metadata.get("subject") != connection.metadata.get("subject")
            )
        ):
            return None
        return connection

    def active(self, owner: str, generation: str) -> bool:
        connection = self._connection(owner, generation)
        return bool(
            connection
            and connection.metadata.get("issuer") == self.config.issuer
            and float(connection.metadata.get("deadline", 0)) > time.time()
        )

    def revoke(self, owner: str, generation: str) -> bool:
        connection = self._connection(owner, generation, require_current_identity=False)
        if connection is None:
            return False
        return self.store.delete(
            owner,
            PROVIDER,
            account_id=connection.account_id,
            expected_metadata=connection.metadata,
        )

    def browser_identity(self, token: str, *, require_active: bool = True) -> tuple[str, str]:
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
                or (require_active and not self.active(owner, generation))
            ):
                raise ValueError("Login revoked")
            return owner, generation
        except (jwt.PyJWTError, ValueError, TypeError):
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None


class OIDCLoginBroker(OIDCLoginRevoker):
    """Serialize offline-token rotation and cap every login at an absolute deadline."""

    def __init__(self, config: OIDCConfig, store: CredentialStore, ttl_hours: int) -> None:
        if config.provider_type != "oidc" or not config.jwks_uri or not config.client_secret:
            raise ValueError("Offline delegation requires confidential OIDC with JWKS")
        if "offline_access" not in config.scopes.split() or not 1 <= ttl_hours <= 720:
            raise ValueError(
                "Offline delegation requires offline_access and a 1–720 hour lifetime"
            )
        super().__init__(config, store)
        self.ttl = ttl_hours * 3600
        self.jwks = _BoundedJWKClient(config.jwks_uri)
        self.client = httpx.Client(timeout=15, follow_redirects=False, trust_env=False)

    def _claims(self, token: str, subject: str, *, allow_expired: bool = False) -> dict[str, Any]:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientError:
            raise DelegationTemporarilyUnavailable(
                "Identity signing keys temporarily unavailable"
            ) from None
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=self.config.issuer,
            options={
                "verify_aud": False,
                "verify_exp": not allow_expired,
                "require": ["iss", "sub", "iat", "exp"],
            },
        )
        if claims.get("sub") != subject or claims.get("azp") != self.config.client_id:
            raise ValueError("OIDC delegation identity changed")
        if claims.get("typ") != "Bearer":
            raise ValueError("An access token is required")
        return claims

    @staticmethod
    def _refresh_token(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("A refresh token is required")
        return value

    def _owner_claims(
        self, tokens: dict[str, Any], owner: str, subject: str, *, allow_expired: bool = False
    ) -> dict[str, Any]:
        claims = self._claims(tokens["access_token"], subject, allow_expired=allow_expired)
        email = claims.get(self.config.email_claim, owner)
        if not isinstance(email, str) or email.lower() != owner:
            raise ValueError("OIDC owner changed")
        return claims

    def capture(
        self, owner: str, subject: str, tokens: dict[str, Any], *, auth_time: int | None = None
    ) -> dict[str, Any]:
        """Capture a refresh credential only from the trusted login code exchange."""
        try:
            refresh = self._refresh_token(tokens["refresh_token"])
            scope = tokens.get("scope", self.config.scopes)
            if not isinstance(scope, str) or "offline_access" not in scope.split():
                raise ValueError("Offline access was not granted")
            claims = self._owner_claims(tokens, owner, subject)
            deadline = time.time() + self.ttl
            refresh_lifetime = tokens.get("refresh_expires_in", 0)
            if isinstance(refresh_lifetime, bool):
                raise ValueError("Invalid refresh lifetime")
            refresh_lifetime = float(refresh_lifetime)
            if not math.isfinite(refresh_lifetime) or refresh_lifetime < 0:
                raise ValueError("Invalid refresh lifetime")
            if refresh_lifetime > 0:
                deadline = min(deadline, time.time() + refresh_lifetime)
            if deadline <= time.time():
                raise ValueError("Offline grant expired")
            secret = self._secret(tokens, claims, refresh)
            generation = secrets.token_hex(24)
            self.store.upsert(
                owner,
                PROVIDER,
                account_id=generation,
                secret=secret,
                metadata={
                    "issuer": self.config.issuer,
                    "subject": subject,
                    "generation": generation,
                    "auth_time": auth_time if auth_time is not None else int(time.time()),
                    "deadline": deadline,
                },
            )
            self.store.upsert(
                owner,
                PROVIDER,
                account_id="current",
                secret={},
                metadata={
                    "generation": generation,
                    "issuer": self.config.issuer,
                    "subject": subject,
                },
            )
            for previous in self.store.list_for_user(owner):
                if (
                    previous.provider == PROVIDER
                    and previous.account_id != "current"
                    and float(previous.metadata.get("deadline", 0)) <= time.time()
                ):
                    self.store.delete(
                        owner,
                        PROVIDER,
                        account_id=previous.account_id,
                        expected_metadata=previous.metadata,
                    )
            state = self.state(owner, generation)
            if state["generation"] != generation:
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            return state
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None

    def known_subject(self, owner: str) -> str | None:
        """Subject of the owner's most recent login grant, even if expired or unrenewable."""
        current = self.store.get(owner, PROVIDER, account_id="current")
        if current is None:
            current = self.store.get(owner, PROVIDER)
        subject = current.metadata.get("subject") if current is not None else None
        return subject if isinstance(subject, str) and subject else None

    def capture_exchanged(self, owner: str, subject: str, token: str) -> dict[str, Any]:
        """Store an offline grant obtained by exchanging a verified access token."""
        try:
            response = self.client.post(
                self.config.token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "subject_token": token,
                    "requested_token_type": "urn:ietf:params:oauth:token-type:refresh_token",
                    "scope": self.config.scopes,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                },
            )
        except httpx.TransportError:
            raise DelegationTemporarilyUnavailable(
                "Identity provider temporarily unavailable"
            ) from None
        except httpx.HTTPError:
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise DelegationTemporarilyUnavailable("Identity provider temporarily unavailable")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        if response.status_code != 200:
            if body.get("error") in ("invalid_grant", "invalid_scope"):
                raise DelegationNotRenewable("Sign in again with offline access")
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        scope = body.get("scope")
        if (
            not isinstance(body.get("refresh_token"), str)
            or not body["refresh_token"]
            or not isinstance(scope, str)
            or "offline_access" not in scope.split()
        ):
            raise DelegationNotRenewable("Sign in again with offline access")
        return self.capture(owner, subject, body)

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

    def state(self, owner: str, generation: str | None = None) -> dict[str, Any]:
        """Renew a client grant, or select a working grant for downstream delegation."""
        if generation is not None:
            return self._state(owner, generation)
        current = self.store.get(owner, PROVIDER, account_id="current")
        if current is None:
            current = self.store.get(owner, PROVIDER)
        if current is None:
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        candidates = [
            current,
            *sorted(
                self.store.list_for_user(owner), key=lambda item: item.created_at, reverse=True
            ),
        ]
        seen = set()
        for candidate in candidates:
            value = candidate.metadata.get("generation")
            if candidate.provider != PROVIDER or not isinstance(value, str) or value in seen:
                continue
            seen.add(value)
            if self.active(owner, value):
                try:
                    return self._state(owner, value)
                except DelegationUnavailable:
                    continue
        raise DelegationUnavailable("Sign in again to renew identity delegation")

    def _state(self, owner: str, generation: str) -> dict[str, Any]:
        if not isinstance(generation, str):
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        connection = self._connection(owner, generation)
        if connection is None:
            raise DelegationUnavailable("Sign in again to renew identity delegation")

        def refresh(connection):
            meta, secret = connection.metadata, connection.secret or {}
            if (
                meta.get("generation") != generation
                or meta.get("issuer") != self.config.issuer
                or float(meta.get("deadline", 0)) <= time.time()
            ):
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            pending = secret.get("pending_tokens")
            if pending is None and float(secret.get("expires_at", 0)) > time.time() + 60:
                return secret
            tokens = pending
            try:
                refresh_token = self._refresh_token(secret["refresh_token"])
                if tokens is not None:
                    rotated = self._refresh_token(tokens.get("refresh_token", refresh_token))
                    pending_claims = self._owner_claims(
                        tokens, owner, str(meta["subject"]), allow_expired=True
                    )
                    if float(pending_claims["exp"]) <= time.time() + 60:
                        refresh_token = rotated
                        tokens = None
                if tokens is None:
                    response = self.client.post(
                        self.config.token_endpoint,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": self.config.client_id,
                            "client_secret": self.config.client_secret,
                        },
                    )
                    if response.status_code >= 500 or response.status_code in (408, 429):
                        raise DelegationTemporarilyUnavailable(
                            "Identity provider temporarily unavailable"
                        )
                    if response.status_code == 400:
                        try:
                            error = response.json()
                        except ValueError:
                            error = {}
                        if isinstance(error, dict) and error.get("error") == "invalid_grant":
                            raise _LoginRefreshRejected(
                                "Sign in again to renew identity delegation"
                            )
                    response.raise_for_status()
                    tokens = response.json()
                    tokens = {
                        **tokens,
                        "refresh_token": self._refresh_token(
                            tokens.get("refresh_token", refresh_token)
                        ),
                    }
                rotated = self._refresh_token(tokens.get("refresh_token", refresh_token))
                claims = self._owner_claims(tokens, owner, str(meta["subject"]))
                return self._secret(tokens, claims, rotated)
            except DelegationTemporarilyUnavailable:
                if tokens is None:
                    raise
                # Keep rotated credentials encrypted, but never vend them before verification.
                return {**secret, "pending_tokens": tokens}
            except httpx.TransportError:
                raise DelegationTemporarilyUnavailable(
                    "Identity provider temporarily unavailable"
                ) from None
            except (
                httpx.HTTPError,
                jwt.PyJWTError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
            ):
                raise DelegationUnavailable("Sign in again to renew identity delegation") from None

        try:
            connection = self.store.refresh_locked(
                owner, PROVIDER, refresh, account_id=connection.account_id
            )
        except _LoginRefreshRejected:
            self.revoke(owner, generation)
            raise DelegationUnavailable("Sign in again to renew identity delegation") from None
        if connection is None or connection.secret is None or not self.active(owner, generation):
            raise DelegationUnavailable("Sign in again to renew identity delegation")
        if connection.secret.get("pending_tokens") is not None:
            raise DelegationTemporarilyUnavailable("Identity signing keys temporarily unavailable")
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
            auth_time=state["auth_time"],
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
