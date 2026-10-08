"""Google Cloud OAuth configuration and short-lived user credentials."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

SCOPES = "openid email https://www.googleapis.com/auth/cloud-platform"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleCloudError(Exception):
    """A Google credential could not be obtained; never contains token material."""


@dataclass(frozen=True)
class GoogleCloudConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> GoogleCloudConfig | None:
        if os.environ.get("OMNIGENT_GOOGLE_CLOUD_AUTH") != "1":
            return None
        values = [
            os.environ.get(f"OMNIGENT_GOOGLE_CLOUD_{key}", "").strip()
            for key in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI")
        ]
        if not any(values):
            return None
        if not all(values):
            raise RuntimeError(
                "Google Cloud OAuth requires CLIENT_ID, CLIENT_SECRET and REDIRECT_URI"
            )
        uri = urlsplit(values[2])
        if uri.scheme != "https" or not uri.netloc or uri.username or uri.fragment:
            raise RuntimeError("Google Cloud OAuth redirect must be an HTTPS URL")
        return cls(*values)


class GoogleCloudClient:
    def __init__(self, config: GoogleCloudConfig) -> None:
        self.config = config

    async def token(self, fields: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        **fields,
                    },
                )
                response.raise_for_status()
                data = response.json()
            token = data.get("access_token")
            lifetime = data.get("expires_in")
            if (
                not isinstance(token, str)
                or not token
                or any(c.isspace() for c in token)
                or type(lifetime) is not int
                or not 60 <= lifetime <= 3600
                or data.get("token_type", "").lower() != "bearer"
            ):
                raise ValueError("Invalid token response")
            return {**data, "expires_at": time.time() + lifetime}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise GoogleCloudError(
                "Google Cloud authorization failed; reconnect your account"
            ) from None

    async def identity(self, access_token: str) -> dict[str, str]:
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(
                    "https://openidconnect.googleapis.com/v1/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                data = response.json()
            if (
                not isinstance(data.get("sub"), str)
                or not data["sub"]
                or not isinstance(data.get("email"), str)
                or not data["email"]
                or data.get("email_verified") is not True
            ):
                raise ValueError("Unverified identity")
            return {"subject": data["sub"], "email": data["email"]}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise GoogleCloudError("Could not verify Google account") from None


async def resolve_google_cloud_credential(
    user_id: str, *, store: Any, client: GoogleCloudClient
) -> dict[str, Any] | None:
    if user_id.startswith("oidc-machine:"):
        return None
    # Re-read after refresh so disconnect/reconnect cannot resurrect old grants.
    connection = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if connection is None:
        return None
    secret = connection.secret or {}
    if connection.metadata.get("expires_at", 0) <= time.time() + 120:
        refresh = secret.get("refresh_token")
        if not refresh:
            raise GoogleCloudError("Reconnect your Google Cloud account")
        try:
            tokens = await client.token({"grant_type": "refresh_token", "refresh_token": refresh})
        except GoogleCloudError:
            # Another request may already have refreshed this same grant.
            updated = False
        else:
            updated = await asyncio.to_thread(
                store.refresh, user_id, tokens=tokens, connection=connection
            )
        current = await asyncio.to_thread(store.get, user_id, with_tokens=True)
        if current is None or current.metadata.get("generation") != connection.metadata.get(
            "generation"
        ):
            return None
        if not updated and (
            current.metadata == connection.metadata
            or current.metadata.get("expires_at", 0) <= time.time() + 5
        ):
            raise GoogleCloudError("Google Cloud refresh temporarily unavailable; retry")
        connection = current
        secret = connection.secret or {}
        expiry = connection.metadata["expires_at"]
    else:
        expiry = connection.metadata["expires_at"]
    current = await asyncio.to_thread(store.get, user_id)
    if current is None or current.metadata.get("generation") != connection.metadata.get(
        "generation"
    ):
        return None
    return {
        "token": secret["access_token"],
        "expires_at": expiry,
        "email": connection.metadata["email"],
        "scopes": SCOPES.split(),
    }
