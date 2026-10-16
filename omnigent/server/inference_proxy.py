"""Session-authorized streaming inference with server-held provider credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import idna
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.inference_upstreams import (
    GoogleInferenceCredential,
    InferenceRoute,
    InferenceUpstream,
    parse_upstreams,
)
from omnigent.stores.host_store import host_is_live

_AUDIENCE = "omnigent-session-inference"
_WORKSPACE_AUDIENCE = "omnigent-inference-workspace"
_TTL = 300
_logger = logging.getLogger(__name__)


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

    upstreams: dict[str, InferenceUpstream] = field(default_factory=dict)
    model_routes: dict[str, InferenceRoute] = field(default_factory=dict)
    model_limits: dict[str, dict[str, int]] = field(default_factory=dict)
    managed_sandboxes: bool = False
    model_reasoning: dict[str, bool] = field(default_factory=dict)

    @property
    def needs_openrouter(self) -> bool:
        return not self.upstreams or any(u.kind == "openrouter" for u in self.upstreams.values())

    def upstream_for(self, model: str) -> tuple[InferenceUpstream, str]:
        if not self.upstreams:
            return InferenceUpstream("openrouter", "https://openrouter.ai/api/v1"), model
        route = self.model_routes[model]
        return self.upstreams[route.upstream], route.model

    def model_apis(self, harness: str) -> dict[str, str]:
        return {
            model: self.upstream_for(model)[0].api
            for model in self.models
            if harness == "pi-native" or self.upstream_for(model)[0].api != "anthropic-messages"
        }

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
            "upstreams",
            "model_routes",
            "model_limits",
            "managed_sandboxes",
            "model_reasoning",
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
        if type(raw.get("managed_sandboxes", False)) is not bool:
            raise ValueError("inference_proxy.managed_sandboxes must be a boolean")
        upstreams, routes = parse_upstreams(raw, tuple(models))
        limits_raw = raw.get("model_limits", {})
        from omnigent.inference_config import parse_inference_config

        parse_inference_config(
            {
                "providers": {"proxy": {}},
                "inference": {
                    "harnesses": {
                        "pi-native": {
                            "provider": "proxy",
                            "model_allowlist": models,
                            "model_limits": limits_raw,
                            "model_reasoning": raw.get("model_reasoning", {}),
                        }
                    }
                },
            }
        )
        config = cls(
            url.rstrip("/"),
            hmac.new(key.encode(), b"omnigent-inference-signing-v1", hashlib.sha256).hexdigest(),
            tuple(models),
            tuple(harnesses),
            upstreams=upstreams,
            model_routes=routes,
            managed_sandboxes=raw.get("managed_sandboxes", False),
            model_limits={model: dict(limits) for model, limits in limits_raw.items()},
            model_reasoning=dict(raw.get("model_reasoning", {})),
            **limits,
        )
        config.validate_harnesses()
        return config

    def validate_harnesses(self) -> None:
        if any(not self.model_apis(harness) for harness in self.harnesses):
            raise ValueError("Each enabled inference harness requires a compatible model")


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
        config.validate_harnesses()
        self.config = config
        self.conversations = conversations
        self.hosts = hosts
        self.broker = broker
        if config.needs_openrouter and broker is None:
            raise ValueError("OpenRouter inference requires openrouter_wif")
        if any(u.kind.startswith("vertex-") for u in config.upstreams.values()):
            try:
                import google.auth.transport.requests  # noqa: F401
            except ImportError:
                raise ValueError("Vertex inference requires omnigent[vertex]") from None
        self.google_credential = GoogleInferenceCredential()
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

    def _protocol_metadata(self, harness: str) -> dict[str, Any]:
        apis = self.config.model_apis(harness)
        if harness != "pi-native":
            return {}
        return {
            **({"model_apis": apis} if "anthropic-messages" in apis.values() else {}),
            **({"model_limits": self.config.model_limits} if self.config.model_limits else {}),
            **(
                {"model_reasoning": self.config.model_reasoning}
                if self.config.model_reasoning
                else {}
            ),
        }

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
                "models": list(self.config.model_apis(harness)),
                **self._protocol_metadata(harness),
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
        if not self.config.model_apis(harness):
            raise ValueError("No compatible inference models for this harness")
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
                "models": list(self.config.model_apis(harness)),
                "harness": harness,
                **self._protocol_metadata(harness),
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
                        **{
                            key: claims[key]
                            for key in ("model_apis", "model_limits", "model_reasoning")
                            if key in claims
                        },
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
            if endpoint not in ("chat/completions", "responses", "v1/messages") or (
                request.url.query
                and not (
                    endpoint == "v1/messages" and request.scope.get("query_string") == b"beta=true"
                )
            ):
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
                selected, target_model = self.config.upstream_for(payload["model"])
                try:
                    target_url, upstream_payload = selected.request(
                        endpoint, payload, target_model
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from None
                provider_headers = {}
                if selected.kind == "vertex-anthropic" and "anthropic-beta" in request.headers:
                    beta = request.headers["anthropic-beta"]
                    if len(beta) > 2048 or re.fullmatch(r"[A-Za-z0-9_, -]+", beta) is None:
                        raise HTTPException(400, "Invalid Anthropic beta features")
                    provider_headers["anthropic-beta"] = beta
                client = self._upstream_client()

                async def setup_upstream():
                    nonlocal upstream
                    try:
                        if selected.kind == "openrouter":
                            credential = await self.broker.credential()
                            upstream_token = str(credential["token"])
                        elif selected.kind.startswith("vertex-"):
                            upstream_token = await self.google_credential.credential()
                        else:
                            upstream_token = os.environ.get(selected.api_key_env or "", "")
                            if (
                                not upstream_token
                                or not upstream_token.isascii()
                                or any(c.isspace() for c in upstream_token)
                            ):
                                raise ValueError("Upstream credential unavailable")
                    except Exception:  # noqa: BLE001 — authentication errors may contain credentials
                        raise HTTPException(502, "Inference authentication unavailable") from None
                    await self.authorize(session_id, access, workspace_id=workspace)
                    upstream = await client.send(
                        client.build_request(
                            "POST",
                            target_url,
                            json=upstream_payload,
                            headers={
                                "Authorization": "Bearer " + upstream_token,
                                "Content-Type": "application/json",
                                "Accept-Encoding": "identity",
                                **provider_headers,
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
                if not watching.cancelled():
                    watching.result()
                    raise HTTPException(499, "Inference client disconnected")
                assert upstream is not None
                response = upstream
                _logger.info(
                    "Inference upstream=%s kind=%s model=%r status=%d",
                    self.config.model_routes[payload["model"]].upstream
                    if self.config.model_routes
                    else "openrouter",
                    selected.kind,
                    payload["model"],
                    response.status_code,
                )
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
