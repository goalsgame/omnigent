"""Operator-owned inference routes and server-only upstream authentication."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


def vertex_base_url(project: object, location: object) -> str:
    """
    The Vertex AI REST base for *project* and *location*.

    :returns: e.g. ``"https://aiplatform.eu.rep.googleapis.com/v1/projects/p/locations/eu"``.
    :raises ValueError: When either value is malformed.
    """
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,20}", project) is None
    ):
        raise ValueError("Invalid Vertex project")
    if (
        not isinstance(location, str)
        or re.fullmatch(r"global|eu|us|[a-z]+-[a-z]+[0-9]+", location) is None
    ):
        raise ValueError("Invalid Vertex location")
    if location == "global":
        host = "aiplatform.googleapis.com"
    elif location in ("eu", "us"):
        host = f"aiplatform.{location}.rep.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{location}"


@dataclass(frozen=True)
class InferenceUpstream:
    kind: str
    base_url: str
    api_key_env: str | None = None

    @property
    def api(self) -> str:
        return "anthropic-messages" if self.kind == "vertex-anthropic" else "openai-completions"

    def serves_anthropic_messages(self, model: str) -> bool:
        """Whether this upstream accepts the Anthropic messages API for ``model``."""
        return self.kind == "vertex-anthropic" or (
            self.kind == "openrouter" and model.startswith("anthropic/")
        )

    @classmethod
    def parse(cls, raw: object) -> InferenceUpstream:
        if not isinstance(raw, dict):
            raise ValueError("Each inference upstream must be a mapping")
        kind = raw.get("kind")
        if kind == "openrouter":
            if set(raw) != {"kind"}:
                raise ValueError("OpenRouter uses the configured openrouter_wif broker")
            return cls(kind, "https://openrouter.ai/api/v1")
        if kind in ("vertex-openai", "vertex-anthropic"):
            if set(raw) != {"kind", "project", "location"}:
                raise ValueError("Vertex upstreams require only kind, project and location")
            return cls(kind, vertex_base_url(raw["project"], raw["location"]))
        if kind == "openai":
            if set(raw) != {"kind", "base_url", "api_key_env"}:
                raise ValueError("OpenAI-compatible upstreams require base_url and api_key_env")
            url, env = raw["base_url"], raw["api_key_env"]
            if (
                not isinstance(url, str)
                or not url.isascii()
                or any(ord(c) < 33 or ord(c) == 127 for c in url)
            ):
                raise ValueError("Inference upstream base_url must be HTTPS")
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError:
                raise ValueError("Invalid inference upstream URL") from None
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or port == 0
                or parsed.username
                or parsed.password
                or "?" in url
                or "#" in url
                or parsed.query
                or parsed.fragment
                or "%" in url
                or any(p in (".", "..") for p in parsed.path.split("/"))
            ):
                raise ValueError(
                    "Inference upstream base_url must be an HTTPS URL without credentials or query"
                )
            if not isinstance(env, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env) is None:
                raise ValueError(
                    "Inference upstream api_key_env must name an environment variable"
                )
            token = os.environ.get(env, "")
            if not token or any(c.isspace() for c in token) or not token.isascii():
                raise ValueError(
                    "Inference upstream api_key_env must resolve to a bearer credential"
                )
            return cls(kind, url.rstrip("/"), env)
        raise ValueError("Unknown inference upstream kind")

    def request(
        self, endpoint: str, payload: dict[str, Any], model: str
    ) -> tuple[str, dict[str, Any]]:
        body = {**payload, "model": model}
        if self.kind == "vertex-anthropic":
            if endpoint != "v1/messages":
                raise ValueError("This model requires the Anthropic messages API")
            if re.fullmatch(r"[A-Za-z0-9._@-]+", model) is None:
                raise ValueError("Invalid Vertex Anthropic model")
            body.pop("model")
            body["anthropic_version"] = "vertex-2023-10-16"
            action = "streamRawPredict" if body.get("stream") else "rawPredict"
            return f"{self.base_url}/publishers/anthropic/models/{model}:{action}", body
        if endpoint == "v1/messages":
            if not self.serves_anthropic_messages(model):
                raise ValueError("This model does not support the Anthropic messages API")
            return f"{self.base_url}/messages", body
        if endpoint not in ("chat/completions", "responses"):
            raise ValueError("This model requires an OpenAI-compatible API")
        if self.kind == "vertex-openai":
            if endpoint != "chat/completions":
                raise ValueError("Vertex OpenAI upstream supports chat completions only")
            from omnigent.server.inference_vertex import restore_thought_signatures

            return f"{self.base_url}/endpoints/openapi/{endpoint}", restore_thought_signatures(
                body
            )
        if self.kind == "openrouter" and endpoint == "chat/completions" and body.get("stream"):
            options = body.get("stream_options")
            if options is not None and not isinstance(options, dict):
                raise ValueError("stream_options must be an object")
            body["stream_options"] = {**(options or {}), "include_usage": True}
        if self.kind == "openrouter" and model.startswith("anthropic/"):
            # Preserve explicit cache settings, including per-block breakpoints.
            if not _has_cache_control(body):
                body["cache_control"] = {"type": "ephemeral"}
        return f"{self.base_url}/{endpoint}", body


def _has_cache_control(body: dict[str, Any]) -> bool:
    if "cache_control" in body:
        return True
    blocks: list[Any] = list(body.get("tools", [])) if isinstance(body.get("tools"), list) else []
    for field in ("messages", "input"):
        messages = body.get(field)
        if not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, dict):
                blocks.append(message)
                content = message.get("content")
                if isinstance(content, list):
                    blocks.extend(content)
    return any(
        isinstance(block, dict)
        and ("cache_control" in block or "prompt_cache_breakpoint" in block)
        for block in blocks
    )


@dataclass(frozen=True)
class InferenceRoute:
    upstream: str
    model: str


def parse_upstreams(
    raw: dict, models: tuple[str, ...]
) -> tuple[dict[str, InferenceUpstream], dict[str, InferenceRoute]]:
    upstreams_raw, routes_raw = raw.get("upstreams"), raw.get("model_routes")
    if upstreams_raw is None and routes_raw is None:
        return {}, {}
    if (
        not isinstance(upstreams_raw, dict)
        or not upstreams_raw
        or not isinstance(routes_raw, dict)
        or set(routes_raw) != set(models)
    ):
        raise ValueError(
            "inference_proxy requires named upstreams and exactly one route per allowed model"
        )
    upstreams = {}
    for name, value in upstreams_raw.items():
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None
        ):
            raise ValueError("Invalid inference upstream name")
        upstreams[name] = InferenceUpstream.parse(value)
    routes = {}
    for alias, value in routes_raw.items():
        if not isinstance(value, dict) or set(value) != {"upstream", "model"}:
            raise ValueError("Inference routes require upstream and model")
        upstream, model = value["upstream"], value["model"]
        if (
            not isinstance(upstream, str)
            or upstream not in upstreams
            or not isinstance(model, str)
            or not model
            or not model.isascii()
            or any(c.isspace() for c in model)
        ):
            raise ValueError("Invalid inference model route")
        if (
            upstreams[upstream].kind == "vertex-anthropic"
            and re.fullmatch(r"[A-Za-z0-9._@-]+", model) is None
        ):
            raise ValueError("Invalid Vertex Anthropic model")
        routes[alias] = InferenceRoute(upstream, model)
    return upstreams, routes


class GoogleInferenceCredential:
    """Cache and refresh ADC access tokens without exposing them to runners."""

    def __init__(self) -> None:
        self._credentials: Any = None
        self._lock = threading.Lock()
        self._refresh_task: asyncio.Task[str] | None = None

    def _token(self) -> str:
        with self._lock:
            return self._locked_token()

    def _locked_token(self) -> str:
        from functools import partial

        import google.auth
        import requests
        from google.auth.transport.requests import Request

        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not self._credentials.valid:
            with requests.Session() as session:
                session.trust_env = False
                self._credentials.refresh(partial(Request(session=session), timeout=10))
        token = self._credentials.token
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Google inference authentication unavailable")
        return token

    @staticmethod
    def _observe_refresh(task: asyncio.Task[str]) -> None:
        # A cancelled HTTP request can leave no waiter to retrieve a failed
        # refresh. Observe it without changing what remaining waiters receive.
        if not task.cancelled():
            task.exception()

    async def credential(self) -> str:
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(self._token))
            task.add_done_callback(self._observe_refresh)
            self._refresh_task = task
        # Waiters consume no executor workers and cancellation cannot interrupt
        # the shared refresh or allow a second refresh to start beside it.
        return await asyncio.shield(task)
