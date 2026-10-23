"""Operator-configured Lore repositories and owner-bound credentials."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from omnigent.lore_urls import lore_remote
from omnigent.server.oidc_mcp import MCPDelegationBroker, MCPDelegationConfig


@dataclass(frozen=True)
class LoreConfig:
    delegation: MCPDelegationConfig
    repositories: dict[str, str | None]
    machine_principals: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> LoreConfig | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("oidc_lore must be a mapping")
        if value.keys() - {
            "upstream_url",
            "audience",
            "scope",
            "machine_client_id",
            "machine_client_secret_env",
            "machine_scope",
            "repositories",
            "machine_principals",
        }:
            raise ValueError("Unknown oidc_lore configuration option")
        delegation = MCPDelegationConfig.parse(value, section="oidc_lore")
        assert delegation is not None
        raw = value.get("repositories")
        if not isinstance(raw, dict) or not raw:
            raise ValueError("oidc_lore.repositories must be a nonempty mapping")
        repositories: dict[str, str | None] = {}
        origin = urlsplit(delegation.upstream_url)
        if origin.path not in ("", "/"):
            raise ValueError("oidc_lore.upstream_url must be an HTTPS origin")
        for remote, options in raw.items():
            if not isinstance(remote, str):
                raise ValueError("Lore repository URLs must be strings")
            if remote.endswith("/*"):
                lore_remote(remote.removesuffix("/*"))
                lore_remote(remote.removesuffix("*") + "x")
            else:
                lore_remote(remote)
            if "*" in remote.removesuffix("/*"):
                raise ValueError("Lore repository URLs only accept a trailing /* wildcard")
            parsed = urlsplit(remote)
            if parsed.netloc != origin.netloc:
                raise ValueError("Lore repositories must match the configured upstream origin")
            if not isinstance(options, dict) or options.keys() - {"view"}:
                raise ValueError("Lore repository options only accept view")
            view = options.get("view")
            if view is not None and (
                not isinstance(view, str) or not view.strip() or "\0" in view or len(view) > 65536
            ):
                raise ValueError("Lore view must be nonempty text of at most 65536 characters")
            repositories[remote] = view
        machines = value.get("machine_principals", [])
        if not isinstance(machines, list) or any(
            not isinstance(owner, str)
            or not owner.startswith("oidc-machine:")
            or not owner.removeprefix("oidc-machine:")
            for owner in machines
        ):
            raise ValueError("Lore machine_principals must list machine owner identities")
        return cls(delegation, repositories, tuple(machines))

    def match_repository(self, remote: str) -> str | None:
        """Return the configured key that admits ``remote``, or None.

        An exact key wins. A key ending in ``/*`` admits a remote that starts with
        the key minus ``*`` followed by exactly one path segment; the longest such
        key wins. Matching is case-sensitive.
        """
        if remote in self.repositories:
            return remote
        if not isinstance(remote, str):
            return None
        try:
            lore_remote(remote)
        except ValueError:
            return None
        best: str | None = None
        for key in self.repositories:
            if not key.endswith("/*"):
                continue
            prefix = key[:-1]
            rest = remote[len(prefix) :]
            if remote.startswith(prefix) and rest and "/" not in rest:
                if best is None or len(key) > len(best):
                    best = key
        return best


class LoreDelegationBroker(MCPDelegationBroker):
    """Store Lore grants separately; never fall back to a shared machine identity."""

    def __init__(self, config: LoreConfig, *args, **kwargs):
        self.lore_config = config
        super().__init__(config.delegation, *args, provider="oidc_lore", **kwargs)

    def _client_credentials(self, owner: str) -> dict[str, str]:
        if owner.startswith("oidc-machine:") and owner not in self.lore_config.machine_principals:
            from omnigent.server.oidc_mcp import DelegationUnavailable

            raise DelegationUnavailable("Machine Lore access is not configured")
        return super()._client_credentials(owner)

    def capture_bearer(self, token: str) -> None:
        verifier = self.auth.machine_verifier
        owner = verifier.authenticate(token) if verifier else None
        if owner and owner not in self.lore_config.machine_principals:
            return
        super().capture_bearer(token)
