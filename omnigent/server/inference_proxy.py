"""Session-authorized streaming inference with server-held provider credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from omnigent.db.db_models import current_workspace_id
from omnigent.stores.host_store import host_is_live

_AUDIENCE = "omnigent-session-inference"
_TTL = 300


@dataclass(frozen=True)
class InferenceProxyConfig:
    public_url: str
    signing_key: str
    models: tuple[str, ...]
    harnesses: tuple[str, ...] = ("openai-agents",)
    max_request_bytes: int = 8 * 1024 * 1024
    max_concurrent_requests: int = 32

    @classmethod
    def parse(cls, raw: object) -> InferenceProxyConfig | None:
        if raw is None:
            return None
        allowed = {
            "public_url",
            "signing_key_env",
            "models",
            "harnesses",
            "max_request_bytes",
            "max_concurrent_requests",
        }
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("Invalid inference_proxy configuration")
        url = raw.get("public_url")
        if not isinstance(url, str):
            raise ValueError("inference_proxy.public_url is required")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("inference_proxy.public_url must be an HTTPS origin")
        env = raw.get("signing_key_env")
        key = os.environ.get(env, "") if isinstance(env, str) else ""
        if len(key.encode()) < 32:
            raise ValueError("inference_proxy.signing_key_env must resolve to at least 32 bytes")
        models = raw.get("models")
        if (
            not isinstance(models, list)
            or not models
            or any(not isinstance(m, str) or not m.strip() for m in models)
        ):
            raise ValueError("inference_proxy.models must be a nonempty model allowlist")
        harnesses = raw.get("harnesses", ["openai-agents"])
        if (
            not isinstance(harnesses, list)
            or not harnesses
            or any(h not in ("openai-agents", "pi-native") for h in harnesses)
        ):
            raise ValueError("inference_proxy supports openai-agents and pi-native")
        limits = {}
        for name, default in (
            ("max_request_bytes", 8 * 1024 * 1024),
            ("max_concurrent_requests", 32),
        ):
            value = raw.get(name, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"inference_proxy.{name} must be a positive integer")
            limits[name] = value
        return cls(
            url.rstrip("/"),
            hmac.new(key.encode(), b"omnigent-inference-signing-v1", hashlib.sha256).hexdigest(),
            tuple(models),
            tuple(harnesses),
            **limits,
        )


class InferenceProxy:
    def __init__(
        self,
        config: InferenceProxyConfig,
        conversations: Any,
        hosts: Any,
        broker: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        machine_allowed: Callable[[str], bool] | None = None,
    ):
        self.config = config
        self.conversations = conversations
        self.hosts = hosts
        self.broker = broker
        self.transport = transport
        self.machine_allowed = machine_allowed
        self.active_requests = 0
        self.active_sessions: set[tuple[int, str]] = set()

    def _grant(
        self, session_id: str, runner_id: str, host_id: str, owner: str, generation: str | None
    ) -> str:
        message = json.dumps(
            [
                "inference-refresh-v1",
                current_workspace_id(),
                session_id,
                runner_id,
                host_id,
                owner,
                generation,
            ],
            separators=(",", ":"),
        )
        return hmac.new(
            self.config.signing_key.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

    def launch_config(
        self,
        conversation: Any,
        runner_id: str,
        harness: str | None,
        host_id: str,
        owner: str | None,
        generation: str | None,
    ) -> dict[str, Any] | None:
        if harness not in self.config.harnesses or not owner:
            return None
        return {
            "server_inference": {
                "url": self.config.public_url + "/v1/inference/sessions/" + conversation.id,
                "grant": self._grant(conversation.id, runner_id, host_id, owner, generation),
                "models": list(self.config.models),
                "harness": harness,
            }
        }

    async def _session(self, session_id: str) -> Any:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or not conv.host_id or not conv.runner_id:
            raise HTTPException(401, "Inference session unavailable")
        host = await asyncio.to_thread(self.hosts.get_host, conv.host_id)
        owner = await asyncio.to_thread(
            self.conversations.get_session_owner_authority, session_id, owner_only=True
        )
        if (
            host is None
            or owner is None
            or host.user_id != owner.user_id
            or host.account_generation != owner.generation
            or not host_is_live(host)
        ):
            raise HTTPException(401, "Inference session unavailable")
        if owner.user_id.startswith("oidc-machine:") and (
            self.machine_allowed is None
            or not await asyncio.to_thread(self.machine_allowed, owner.user_id)
        ):
            raise HTTPException(401, "Inference session unavailable")
        return conv, owner

    async def authorize(self, session_id: str, token: str, *, refresh: bool = False) -> Any:
        import jwt

        conv, owner = await self._session(session_id)
        grant = self._grant(
            session_id, conv.runner_id, conv.host_id, owner.user_id, owner.generation
        )
        binding = hashlib.sha256(grant.encode()).hexdigest()
        if refresh:
            valid = hmac.compare_digest(token, grant)
        else:
            try:
                claims = jwt.decode(
                    token,
                    self.config.signing_key,
                    algorithms=["HS256"],
                    audience=_AUDIENCE,
                    options={"require": ["exp", "iat", "sub", "runner", "host"]},
                )
                valid = (
                    claims["sub"] == session_id
                    and claims["runner"] == conv.runner_id
                    and claims["host"] == conv.host_id
                    and claims.get("workspace") == current_workspace_id()
                    and claims.get("binding") == binding
                )
            except jwt.PyJWTError:
                valid = False
        if not valid:
            raise HTTPException(401, "Invalid inference credential")
        return conv, binding

    def router(self) -> APIRouter:
        router = APIRouter()

        def bearer(request: Request) -> str:
            value = request.headers.get("authorization", "")
            if not value.startswith("Bearer ") or not value.isascii() or len(value) > 4096:
                raise HTTPException(401, "Inference bearer required")
            return value[7:]

        @router.post("/inference/sessions/{session_id}/token")
        async def token(request: Request, session_id: str) -> JSONResponse:
            import jwt

            conv, binding = await self.authorize(session_id, bearer(request), refresh=True)
            now = int(time.time())
            encoded = jwt.encode(
                {
                    "aud": _AUDIENCE,
                    "sub": session_id,
                    "runner": conv.runner_id,
                    "host": conv.host_id,
                    "workspace": current_workspace_id(),
                    "binding": binding,
                    "iat": now,
                    "exp": now + _TTL,
                },
                self.config.signing_key,
                algorithm="HS256",
            )
            return JSONResponse(
                {"token": encoded, "expires_in": _TTL}, headers={"Cache-Control": "no-store"}
            )

        @router.get("/inference/sessions/{session_id}/models")
        async def models(request: Request, session_id: str) -> JSONResponse:
            await self.authorize(session_id, bearer(request))
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "owned_by": "server"}
                        for model in self.config.models
                    ],
                },
                headers={"Cache-Control": "no-store"},
            )

        @router.post("/inference/sessions/{session_id}/{endpoint:path}")
        async def inference(request: Request, session_id: str, endpoint: str) -> StreamingResponse:
            if endpoint not in ("chat/completions", "responses") or request.url.query:
                raise HTTPException(404, "Unsupported inference endpoint")
            access = bearer(request)
            await self.authorize(session_id, access)
            if request.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(415, "Compressed requests are unsupported")
            body = bytearray()
            try:
                async with asyncio.timeout(30):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > self.config.max_request_bytes:
                            raise HTTPException(413, "Inference request too large")
            except TimeoutError:
                raise HTTPException(408, "Inference request timed out") from None
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(400, "Invalid inference JSON") from None
            if (
                not isinstance(payload, dict)
                or payload.get("model") not in self.config.models
                or any(key in payload for key in ("models", "route"))
            ):
                raise HTTPException(403, "Model is not permitted")
            if endpoint == "responses":
                if any(
                    payload.get(key)
                    for key in ("previous_response_id", "conversation", "background", "store")
                ):
                    raise HTTPException(400, "Only stateless inference is supported")
                payload["store"] = False
            session_key = (current_workspace_id(), session_id)
            if (
                session_key in self.active_sessions
                or self.active_requests >= self.config.max_concurrent_requests
            ):
                raise HTTPException(429, "Inference capacity exhausted")
            self.active_requests += 1
            self.active_sessions.add(session_key)
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(180, connect=10),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            )
            upstream = None
            released = False

            async def cleanup():
                nonlocal released
                if released:
                    return
                released = True
                self.active_requests -= 1
                self.active_sessions.discard(session_key)
                with anyio.CancelScope(shield=True):
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                    finally:
                        await client.aclose()

            try:
                credential = await self.broker.credential()
                await self.authorize(session_id, access)
                upstream = await client.send(
                    client.build_request(
                        "POST",
                        "https://openrouter.ai/api/v1/" + endpoint,
                        json=payload,
                        headers={
                            "Authorization": "Bearer " + str(credential["token"]),
                            "Content-Type": "application/json",
                            "Accept-Encoding": "identity",
                        },
                    ),
                    stream=True,
                )
                if upstream.status_code != 200:
                    raise HTTPException(
                        429 if upstream.status_code == 429 else 502,
                        "Inference provider rejected the request",
                    )
            except BaseException as exc:
                await cleanup()
                if isinstance(exc, (httpx.HTTPError, RuntimeError)):
                    raise HTTPException(502, "Inference provider unavailable") from None
                raise

            async def chunks():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await cleanup()

            class InferenceResponse(StreamingResponse):
                async def __call__(self, scope, receive, send):
                    try:
                        await super().__call__(scope, receive, send)
                    finally:
                        await cleanup()

            return InferenceResponse(
                chunks(),
                media_type=upstream.headers.get("content-type", "application/json"),
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        return router
