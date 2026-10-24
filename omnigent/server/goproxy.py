"""Operator-configured Go module proxy and owner-bound credentials for it."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from omnigent.server.oidc_mcp import (
    DelegationUnavailable,
    MCPDelegationBroker,
    MCPDelegationConfig,
)


def _text(value: dict, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"oidc_goproxy.{key} must be a non-empty string")
    return item.strip()


@dataclass(frozen=True)
class GoProxyConfig:
    url: str
    prefix: str
    audience: str
    scope: str

    @classmethod
    def parse(cls, value: object) -> GoProxyConfig | None:
        """Load ``oidc_goproxy``; ``None`` leaves the provider off."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("oidc_goproxy must be a mapping")
        if value.keys() - {"url", "prefix", "audience", "scope"}:
            raise ValueError("Unknown oidc_goproxy configuration option")
        url = _text(value, "url").rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise ValueError("oidc_goproxy.url must be an HTTPS origin")
        prefix = _text(value, "prefix") if "prefix" in value else parsed.hostname
        if "://" in prefix or any(c.isspace() or c in "\\?#" for c in prefix):
            raise ValueError("oidc_goproxy.prefix must be a Go module path prefix")
        return cls(url, prefix.strip("/"), _text(value, "audience"), _text(value, "scope"))

    def matches(self, url: str | None) -> bool:
        """True when ``url`` names the configured proxy; no other URL is admitted."""
        return isinstance(url, str) and url.rstrip("/") == self.url


class GoProxyBroker(MCPDelegationBroker):
    """Vend short-lived proxy tokens for a signed-in person; machines never qualify."""

    def __init__(self, config: GoProxyConfig, *args, **kwargs):
        self.goproxy_config = config
        delegation = MCPDelegationConfig(config.url, config.audience, config.scope, "", "")
        super().__init__(delegation, *args, provider="oidc_goproxy", **kwargs)

    def _client_credentials(self, owner: str) -> dict[str, str]:
        if owner.startswith("oidc-machine:"):
            raise DelegationUnavailable("Go proxy access is limited to signed-in people")
        return super()._client_credentials(owner)
