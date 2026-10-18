"""Renew browser access from a separately signed, bounded OIDC login cookie."""

from __future__ import annotations

import asyncio
import time
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.server.oidc_login import OIDCLoginBroker
from omnigent.server.oidc_mcp import DelegationUnavailable


class OIDCLoginMiddleware:
    def __init__(self, app: ASGIApp, broker: OIDCLoginBroker) -> None:
        self.app, self.broker = app, broker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        renewal = request.cookies.get(self.broker.cookie_name)
        if (
            not renewal
            or request.headers.get("authorization")
            or request.url.path.startswith(("/auth/", "/assets/"))
            or request.url.path == "/health"
        ):
            await self.app(scope, receive, send)
            return
        try:
            owner, generation = await asyncio.to_thread(self.broker.browser_identity, renewal)
            current = request.cookies.get(self.broker.config.session_cookie_name)
            try:
                claims = jwt.decode(
                    current or "", self.broker.config.cookie_secret, algorithms=["HS256"]
                )
            except jwt.PyJWTError:
                claims = {}
            if (
                claims.get("sub") == owner
                and claims.get("oidc_grant_id") == generation
                and float(claims.get("exp", 0)) > time.time() + 60
            ):
                await self.app(scope, receive, send)
                return
            state = await asyncio.to_thread(self.broker.state, owner)
            if state["generation"] != generation:
                raise DelegationUnavailable("Sign in again to renew identity delegation")
            token = self.broker.session_token(owner, state)
        except DelegationUnavailable:
            response = JSONResponse(
                {
                    "detail": "Sign in again to restore connector and repository access",
                    "code": "identity_login_required",
                },
                status_code=401,
            )
            if request.method == "GET" and request.url.path.startswith(("/c/", "/artifacts/")):
                base = getattr(request.app.state, "base_path", "")
                response = RedirectResponse(
                    base + "/auth/login?" + urlencode({"return_to": base + request.url.path}),
                    status_code=302,
                )
            response.delete_cookie(
                self.broker.cookie_name,
                path="/",
                secure=self.broker.config.secure_cookies,
                httponly=True,
                samesite="lax",
            )
            response.delete_cookie(
                self.broker.config.session_cookie_name,
                path="/",
                secure=self.broker.config.secure_cookies,
                httponly=True,
                samesite="lax",
            )
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return
        cookies = SimpleCookie()
        cookies.load(request.headers.get("cookie", ""))
        cookies[self.broker.config.session_cookie_name] = token
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"cookie"]
        headers.append((b"cookie", cookies.output(header="", sep=";").strip().encode("latin-1")))
        scope = {**scope, "headers": headers}
        cookie_response = Response()
        cookie_response.set_cookie(
            self.broker.config.session_cookie_name,
            token,
            max_age=self.broker.access_ttl(state),
            path="/",
            secure=self.broker.config.secure_cookies,
            httponly=True,
            samesite="lax",
        )

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"set-cookie", cookie_response.headers["set-cookie"].encode("latin-1")),
                ]
                message["headers"] += [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, send_with_cookie)
