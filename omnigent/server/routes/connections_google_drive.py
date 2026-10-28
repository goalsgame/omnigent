"""Connect a human's Google Drive and let them pick files from it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response

from omnigent.connections.google_drive import GoogleDriveConnectionStore
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.google_drive import (
    DRIVE_SCOPE,
    SCOPES,
    GoogleDriveClient,
    GoogleDriveConfig,
    GoogleDriveError,
    resolve_google_drive_token,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)


def credential_user(request: Request, auth_provider: AuthProvider | None) -> str:
    """Return the directly authenticated human, who alone may use or grant their Drive."""
    require_user(request, auth_provider)
    user = auth_provider.get_credential_user_id(request) if auth_provider else None
    if user is None or user == RESERVED_USER_LOCAL:
        raise HTTPException(403, "Google Drive access requires server authentication")
    if user.startswith("oidc-machine:"):
        raise HTTPException(403, "Google Drive access requires a human account")
    return user


class GoogleDriveConnectionHooks:
    provider = "google_drive"

    def __init__(
        self,
        config: GoogleDriveConfig,
        store: GoogleDriveConnectionStore,
        client: GoogleDriveClient,
    ) -> None:
        self.config, self.store, self.api = config, store, client

    def signing_key(self) -> bytes:
        return hmac.new(
            self.config.client_secret.encode(), b"omnigent.google-drive.state.v1", hashlib.sha256
        ).digest()

    def verifier(self, nonce: str) -> str:
        value = hmac.new(self.signing_key(), b"pkce:" + nonce.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {
            "email": connection.metadata.get("email") if connection else None,
            "generation": connection.metadata.get("generation") if connection else None,
        }

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
        if user_id == RESERVED_USER_LOCAL or user_id.startswith("oidc-machine:"):
            raise ConnectionError("Google Drive requires a human account")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ConnectionError("Invalid Google Drive OAuth state")
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
                raise GoogleDriveError("Google did not issue an offline grant")
            scope = tokens.get("scope")
            if not isinstance(scope, str) or DRIVE_SCOPE not in scope.split():
                raise GoogleDriveError("Google Drive scope was not granted")
            identity = await self.api.identity(tokens["access_token"])
            await asyncio.to_thread(self.store.upsert, user_id, tokens=tokens, identity=identity)
        except GoogleDriveError:
            raise ConnectionError(
                "Google Drive connection failed; grant the requested permissions"
            ) from None


def create_connections_google_drive_router(
    config: GoogleDriveConfig,
    store: GoogleDriveConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: GoogleDriveClient | None = None,
) -> APIRouter:
    api = client or GoogleDriveClient(config)

    def extra_routes(router: APIRouter) -> None:
        @router.get("/connections/google_drive/files")
        async def pick_files(
            request: Request,
            response: Response,
            q: str = Query(default="", max_length=200),
            limit: int = Query(default=20, ge=1, le=50),
        ) -> dict[str, Any]:
            """Search the caller's own Drive so they can choose files to grant."""
            response.headers["Cache-Control"] = "private, no-store"
            user = credential_user(request, auth_provider)
            try:
                token = await resolve_google_drive_token(user, store=store, client=api)
                if token is None:
                    raise HTTPException(409, "Connect Google Drive first")
                return {"files": await api.search(token["token"], q, limit)}
            except GoogleDriveError as exc:
                raise HTTPException(exc.status, str(exc)) from None

    return create_connection_router(
        GoogleDriveConnectionHooks(config, store, api),
        auth_provider=auth_provider,
        extra_routes=extra_routes,
    )
