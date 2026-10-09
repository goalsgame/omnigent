"""Connect a human's Google account using the shared OAuth state binding."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

from omnigent.connections.google_cloud import GoogleCloudConnectionStore
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.google_cloud import (
    SCOPES,
    GoogleCloudClient,
    GoogleCloudConfig,
    GoogleCloudError,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)


class GoogleCloudAccessDecision(BaseModel):
    decision: Literal["allowed", "denied"]
    generation: str


class GoogleCloudConnectionHooks:
    provider = "google_cloud"

    def __init__(
        self,
        config: GoogleCloudConfig,
        store: GoogleCloudConnectionStore,
        client: GoogleCloudClient,
    ) -> None:
        self.config, self.store, self.api = config, store, client

    def signing_key(self) -> bytes:
        return hmac.new(
            self.config.client_secret.encode(), b"omnigent.google-cloud.state.v1", hashlib.sha256
        ).digest()

    def verifier(self, nonce: str) -> str:
        value = hmac.new(self.signing_key(), b"pkce:" + nonce.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {"email": connection.metadata.get("email") if connection else None}

    def begin(self, request: Request, build_state: Any) -> ConnectStart:
        del request
        nonce = secrets.token_urlsafe(24)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier(nonce).encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return ConnectStart(
            "https://accounts.google.com/o/oauth2/v2/auth?"
            + urlencode(
                {
                    "client_id": self.config.client_id,
                    "redirect_uri": self.config.redirect_uri,
                    "response_type": "code",
                    "scope": SCOPES,
                    "access_type": "offline",
                    "prompt": "consent select_account",
                    "include_granted_scopes": "false",
                    "state": build_state({"nonce": nonce}),
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        if user_id.startswith("oidc-machine:"):
            raise ConnectionError("Machine identities require a configured workload binding")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ConnectionError("Invalid Google Cloud OAuth state")
        try:
            tokens = await self.api.token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "code_verifier": self.verifier(nonce),
                }
            )
            if not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]:
                raise GoogleCloudError("Google did not issue an offline grant")
            scope = tokens.get("scope")
            if not isinstance(scope, str) or (
                "https://www.googleapis.com/auth/cloud-platform" not in scope.split()
            ):
                raise GoogleCloudError("Google Cloud scope was not granted")
            identity = await self.api.identity(tokens["access_token"])
            await asyncio.to_thread(self.store.upsert, user_id, tokens=tokens, identity=identity)
        except GoogleCloudError:
            raise ConnectionError(
                "Google Cloud connection failed; grant the requested permissions"
            ) from None


def create_connections_google_cloud_router(
    config: GoogleCloudConfig,
    store: GoogleCloudConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: GoogleCloudClient | None = None,
):
    router = create_connection_router(
        GoogleCloudConnectionHooks(config, store, client or GoogleCloudClient(config)),
        auth_provider=auth_provider,
    )

    @router.get("/connections/google_cloud/sessions/{session_id}/access")
    async def session_access(session_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        require_user(request, auth_provider)
        user = auth_provider.get_credential_user_id(request) if auth_provider else None
        if user is None or user == RESERVED_USER_LOCAL:
            raise HTTPException(403, "Google Cloud sandbox access requires server authentication")
        if user.startswith("oidc-machine:"):
            raise HTTPException(403, "Human session owner required")
        try:
            return await asyncio.to_thread(store.access.session, session_id, user)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/connections/google_cloud/sessions/{session_id}/access")
    async def decide_access(
        session_id: str, body: GoogleCloudAccessDecision, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        require_user(request, auth_provider)
        user = auth_provider.get_credential_user_id(request) if auth_provider else None
        if user is None or user == RESERVED_USER_LOCAL:
            raise HTTPException(403, "Google Cloud sandbox access requires server authentication")
        if user.startswith("oidc-machine:"):
            raise HTTPException(403, "Human session owner required")
        try:
            result = await asyncio.to_thread(
                store.access.session,
                session_id,
                user,
                decision=body.decision,
                generation=body.generation,
            )
            from omnigent.server.google_cloud_approval import settle_google_cloud_access

            settle_google_cloud_access(session_id, user, body.generation, body.decision)
            return result
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    return router
