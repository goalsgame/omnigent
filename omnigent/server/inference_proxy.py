"""Session-authorized streaming inference with server-held provider credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import idna
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.stores.host_store import host_is_live

_AUDIENCE = "omnigent-session-inference"
_WORKSPACE_AUDIENCE = "omnigent-inference-workspace"
_TTL = 300


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


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
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            port = parts.port
            if hostname:
                try:
                    ipaddress.ip_address(hostname)
                except ValueError:
                    idna.encode(hostname, uts46=True, std3_rules=True)
        except ValueError as exc:
            raise ValueError("inference_proxy.public_url must be an HTTPS origin") from exc
        if (
            parts.scheme != "https"
            or not hostname
            or port == 0
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
        self._client: httpx.AsyncClient | None = None

    def _upstream_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(180, connect=10),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
                limits=httpx.Limits(
                    max_connections=self.config.max_concurrent_requests,
                    max_keepalive_connections=self.config.max_concurrent_requests,
                    keepalive_expiry=60,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

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

    def _workspace_route(self, session_id: str, runner_id: str, harness: str) -> str:
        import jwt

        return jwt.encode(
            {
                "aud": _WORKSPACE_AUDIENCE,
                "sub": session_id,
                "workspace": current_workspace_id(),
                "runner": runner_id,
                "origin": self.config.public_url,
                "harness": harness,
                "models": list(self.config.models),
            },
            self.config.signing_key,
            algorithm="HS256",
        )

    def _request_workspace(self, session_id: str, route: str | None) -> int:
        import jwt

        if route is None:
            return current_workspace_id()
        try:
            claims = jwt.decode(
                route,
                self.config.signing_key,
                algorithms=["HS256"],
                audience=_WORKSPACE_AUDIENCE,
                options={"require": ["sub", "workspace"]},
            )
            workspace = claims["workspace"]
            if claims["sub"] != session_id or type(workspace) is not int or workspace < 0:
                raise ValueError("Invalid workspace selector")
            return workspace
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, "Invalid inference workspace") from None

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
        path = (
            "/v1/inference/workspaces/"
            + self._workspace_route(conversation.id, runner_id, harness)
            + "/sessions/"
            + conversation.id
        )
        return {
            "server_inference": {
                "url": self.config.public_url + path,
                "grant": self._grant(conversation.id, runner_id, host_id, owner, generation),
                "models": list(self.config.models),
                "harness": harness,
            }
        }

    def _owner_authority(self, conv: Any) -> Any:
        owner = self.conversations.get_session_owner_authority(conv.id, owner_only=True)
        root = getattr(conv, "root_conversation_id", None)
        if owner is None and root and root != conv.id:
            owner = self.conversations.get_session_owner_authority(root, owner_only=True)
        return owner

    async def _session(self, session_id: str) -> Any:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or not conv.host_id or not conv.runner_id:
            raise HTTPException(401, "Inference session unavailable")
        host = await asyncio.to_thread(self.hosts.get_host, conv.host_id)
        owner = await asyncio.to_thread(self._owner_authority, conv)
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

    def _verify_runner_lineage(self, binding: Any, target_id: str) -> None:
        """Require the target and its ancestor binding to share runner and ownership."""
        from omnigent.stores.conversation_store import (
            SIDE_CHAT_LABEL_KEY,
            SIDE_CHAT_SOURCE_LABEL_KEY,
        )

        target = self.conversations.get_conversation(target_id)
        if target is None or target.archived or target.runner_id != binding.runner_id:
            raise HTTPException(401, "Invalid inference lineage")
        target_owner = self._owner_authority(target)
        binding_owner = self._owner_authority(binding)
        if target_owner != binding_owner or target_owner is None:
            raise HTTPException(401, "Invalid inference lineage")
        current = target
        visited: set[str] = set()
        for _ in range(32):
            if current.id == binding.id:
                return
            if current.id in visited:
                break
            visited.add(current.id)
            ancestor_id = (
                current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
                if current.kind == "default" and current.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
                else current.parent_conversation_id
            )
            if not ancestor_id:
                break
            ancestor = self.conversations.get_conversation(ancestor_id)
            if (
                ancestor is None
                or ancestor.archived
                or ancestor.runner_id != binding.runner_id
                or self._owner_authority(ancestor) != binding_owner
            ):
                break
            current = ancestor
        raise HTTPException(401, "Invalid inference lineage")

    async def authorize(
        self,
        session_id: str,
        token: str,
        *,
        refresh: bool = False,
        workspace_id: int | None = None,
    ) -> Any:
        workspace = current_workspace_id() if workspace_id is None else workspace_id
        with workspace_scope(workspace):
            return await self._authorize_scoped(session_id, token, refresh=refresh)

    async def _authorize_scoped(self, session_id: str, token: str, *, refresh: bool) -> Any:
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

        @router.post("/inference/workspaces/{workspace_route}/sessions/{session_id}/token")
        @router.post("/inference/sessions/{session_id}/token")
        async def token(
            request: Request,
            session_id: str,
            workspace_route: str | None = None,
            runner_session_id: str | None = None,
        ) -> JSONResponse:
            import jwt

            workspace = self._request_workspace(session_id, workspace_route)
            conv, binding = await self.authorize(
                session_id, bearer(request), refresh=True, workspace_id=workspace
            )
            if runner_session_id is not None:
                with workspace_scope(workspace):
                    await asyncio.to_thread(self._verify_runner_lineage, conv, runner_session_id)
            now = int(time.time())
            encoded = jwt.encode(
                {
                    "aud": _AUDIENCE,
                    "sub": session_id,
                    "runner": conv.runner_id,
                    "host": conv.host_id,
                    "workspace": workspace,
                    "binding": binding,
                    "iat": now,
                    "exp": now + _TTL,
                },
                self.config.signing_key,
                algorithm="HS256",
            )
            payload: dict[str, Any] = {"token": encoded, "expires_in": _TTL}
            if workspace_route is not None:
                claims = jwt.decode(
                    workspace_route,
                    self.config.signing_key,
                    algorithms=["HS256"],
                    audience=_WORKSPACE_AUDIENCE,
                )
                if claims.get("runner") is not None:
                    if claims["runner"] != conv.runner_id:
                        raise HTTPException(401, "Invalid inference binding")
                    payload["runtime_binding"] = {
                        "url": claims["origin"]
                        + "/v1/inference/workspaces/"
                        + workspace_route
                        + "/sessions/"
                        + session_id,
                        "session_id": session_id,
                        "runner_id": conv.runner_id,
                        "models": claims["models"],
                        "harness": claims["harness"],
                        "grant_hash": binding,
                        **(
                            {"runner_session_id": runner_session_id}
                            if runner_session_id is not None
                            else {}
                        ),
                    }
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})

        @router.get("/inference/workspaces/{workspace_route}/sessions/{session_id}/models")
        @router.get("/inference/sessions/{session_id}/models")
        async def models(
            request: Request, session_id: str, workspace_route: str | None = None
        ) -> JSONResponse:
            workspace = self._request_workspace(session_id, workspace_route)
            await self.authorize(session_id, bearer(request), workspace_id=workspace)
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

        @router.post(
            "/inference/workspaces/{workspace_route}/sessions/{session_id}/{endpoint:path}"
        )
        @router.post("/inference/sessions/{session_id}/{endpoint:path}")
        async def inference(
            request: Request, session_id: str, endpoint: str, workspace_route: str | None = None
        ) -> StreamingResponse:
            if endpoint not in ("chat/completions", "responses") or request.url.query:
                raise HTTPException(404, "Unsupported inference endpoint")
            workspace = self._request_workspace(session_id, workspace_route)
            access = bearer(request)
            await self.authorize(session_id, access, workspace_id=workspace)
            if request.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(415, "Compressed requests are unsupported")
            session_key = (workspace, session_id)
            if (
                session_key in self.active_sessions
                or self.active_requests >= self.config.max_concurrent_requests
            ):
                raise HTTPException(429, "Inference capacity exhausted")
            self.active_requests += 1
            self.active_sessions.add(session_key)
            upstream: httpx.Response | None = None
            released = False

            async def cleanup():
                nonlocal released
                if released:
                    return
                released = True
                self.active_requests -= 1
                self.active_sessions.discard(session_key)
                with anyio.CancelScope(shield=True):
                    if upstream is not None:
                        await upstream.aclose()

            try:
                body = bytearray()
                try:
                    async with asyncio.timeout(30):
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > self.config.max_request_bytes:
                                raise HTTPException(413, "Inference request too large")
                            body.extend(chunk)
                except TimeoutError:
                    raise HTTPException(408, "Inference request timed out") from None
                try:
                    payload = json.loads(
                        body, parse_constant=_reject_json_constant, parse_float=_finite_json_float
                    )
                except (ValueError, UnicodeDecodeError, RecursionError):
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
                client = self._upstream_client()

                async def setup_upstream():
                    nonlocal upstream
                    credential = await self.broker.credential()
                    await self.authorize(session_id, access, workspace_id=workspace)
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

                async def disconnected():
                    while True:
                        if (await request.receive())["type"] == "http.disconnect":
                            return

                preparing = asyncio.create_task(setup_upstream())
                watching = asyncio.create_task(disconnected())
                try:
                    done, _ = await asyncio.wait(
                        (preparing, watching), return_when=asyncio.FIRST_COMPLETED
                    )
                    if watching in done:
                        raise HTTPException(499, "Inference client disconnected")
                    await preparing
                finally:
                    for task in (preparing, watching):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(preparing, watching, return_exceptions=True)
                assert upstream is not None
                response = upstream
                if response.status_code != 200:
                    raise HTTPException(
                        429 if response.status_code == 429 else 502,
                        "Inference provider rejected the request",
                    )
            except BaseException as exc:
                await cleanup()
                if isinstance(exc, (httpx.HTTPError, RuntimeError)):
                    raise HTTPException(502, "Inference provider unavailable") from None
                raise

            async def chunks():
                try:
                    async for chunk in response.aiter_bytes():
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
                media_type=response.headers.get("content-type", "application/json"),
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        return router
