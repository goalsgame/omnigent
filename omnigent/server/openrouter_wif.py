"""Exchange the server's workload identity for short-lived OpenRouter tokens."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
_TOKEN_URL = "https://openrouter.ai/api/v1/oauth/token"


@dataclass(frozen=True)
class OpenRouterWIFConfig:
    policy_id: str
    audience: str = "https://openrouter.ai"
    subject_token_file: str | None = None

    @classmethod
    def parse(cls, raw: object) -> OpenRouterWIFConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) - {"policy_id", "audience", "subject_token_file"}:
            raise ValueError("openrouter_wif must contain only supported configuration fields")
        policy = raw.get("policy_id")
        audience = raw.get("audience", "https://openrouter.ai")
        token_file = raw.get("subject_token_file")
        if not isinstance(policy, str) or not policy.strip():
            raise ValueError("openrouter_wif.policy_id must be a non-empty string")
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError("openrouter_wif.audience must be a non-empty string")
        if token_file is not None and (
            not isinstance(token_file, str) or not Path(token_file).is_absolute()
        ):
            raise ValueError("openrouter_wif.subject_token_file must be an absolute path")
        return cls(policy.strip(), audience.strip(), token_file)


class OpenRouterWIFError(RuntimeError):
    """A sanitized exchange error, safe to return without upstream response bodies."""


class OpenRouterWIFBroker:
    """Cache tokens in memory and serialize refreshes within one server replica."""

    def __init__(
        self, config: OpenRouterWIFConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._transport = transport
        self._lock = asyncio.Lock()
        self._token = ""
        self._expires_at = 0.0

    async def credential(self) -> dict[str, object]:
        async with self._lock:
            if time.monotonic() >= self._expires_at - 120:
                await self._refresh()
            return {
                "token": self._token,
                "expires_in": max(0, int(self._expires_at - time.monotonic())),
            }

    async def _refresh(self) -> None:
        started = time.monotonic()
        try:
            # Never send a metadata identity or access token through ambient proxies.
            async with httpx.AsyncClient(
                timeout=3.0, trust_env=False, follow_redirects=False, transport=self._transport
            ) as client:
                if self.config.subject_token_file:
                    subject = (
                        await asyncio.to_thread(Path(self.config.subject_token_file).read_text)
                    ).strip()
                else:
                    identity = await client.get(
                        _METADATA_URL,
                        params={"audience": self.config.audience},
                        headers={"Metadata-Flavor": "Google"},
                    )
                    identity.raise_for_status()
                    subject = identity.text.strip()
                if not subject or len(subject.encode()) > 16384:
                    raise ValueError("invalid subject token")
                response = await client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                        "federation_policy_id": self.config.policy_id,
                        "subject_token": subject,
                    },
                )
                response.raise_for_status()
                result = response.json()
                token = result.get("access_token")
                ttl = result.get("expires_in")
                if (
                    not isinstance(token, str)
                    or not token
                    or any(c.isspace() for c in token)
                    or type(ttl) is not int
                    or not 120 < ttl <= 900
                    or result.get("token_type", "").lower() != "bearer"
                ):
                    raise ValueError("invalid token response")
                self._token = token
                self._expires_at = started + ttl
        except (httpx.HTTPError, OSError, ValueError, AttributeError, TypeError):
            # Response bodies and exception messages may contain either bearer.
            raise OpenRouterWIFError("OpenRouter workload identity exchange failed") from None
