"""Repository-scoped GitHub installation credentials for bound OIDC machines."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.oidc_machine_auth import MACHINE_PRINCIPAL_PREFIX


@dataclass(frozen=True)
class GitCommitAuthor:
    name: str
    email: str


@dataclass(frozen=True)
class GitHubMachineBinding:
    installation_id: int
    repository_ids: tuple[int, ...]
    access: str
    ci_read: bool = False
    issues_access: str = "none"
    commit_author: GitCommitAuthor | None = None

    @property
    def permissions(self) -> dict[str, str]:
        permissions = {
            "contents": self.access,
            "pull_requests": self.access,
            "metadata": "read",
        }
        if self.issues_access != "none":
            permissions["issues"] = self.issues_access
        if self.ci_read:
            permissions.update({"checks": "read", "actions": "read", "statuses": "read"})
        return permissions


def parse_machine_bindings(
    value: object, *, principals: frozenset[str]
) -> dict[str, GitHubMachineBinding]:
    """Require explicit installation, repositories and access for each known machine."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not value or len(value) > 128:
        raise RuntimeError("github_machine_auth must be a nonempty machine binding mapping")
    result = {}
    for principal, raw in value.items():
        if (
            not isinstance(principal, str)
            or not principal.startswith(MACHINE_PRINCIPAL_PREFIX)
            or principal not in principals
        ):
            raise RuntimeError("github_machine_auth must bind configured OIDC machine principals")
        required = {
            "installation_id",
            "repository_ids",
            "access",
        }
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or set(raw) - required - {"ci_read", "issues_access", "commit_author"}
        ):
            raise RuntimeError(
                "GitHub machine bindings require installation_id, repository_ids, access "
                "and optionally ci_read, issues_access, commit_author"
            )
        installation = raw["installation_id"]
        repos = raw["repository_ids"]
        access = raw["access"]
        ci_read = raw.get("ci_read", False)
        if type(installation) is not int or installation <= 0:
            raise RuntimeError("GitHub installation_id must be a positive integer")
        if (
            not isinstance(repos, list)
            or not 1 <= len(repos) <= 500
            or any(type(repo) is not int or repo <= 0 for repo in repos)
            or len(set(repos)) != len(repos)
        ):
            raise RuntimeError(
                "GitHub repository_ids must contain 1–500 distinct positive integers"
            )
        if access not in ("read", "write"):
            raise RuntimeError("GitHub machine access must be read or write")
        if type(ci_read) is not bool:
            raise RuntimeError("GitHub machine ci_read must be a boolean")
        issues_access = raw.get("issues_access", "none")
        if issues_access not in ("none", "read", "write"):
            raise RuntimeError("GitHub machine issues_access must be none, read or write")
        commit_author = None
        if "commit_author" in raw:
            author = raw["commit_author"]
            if (
                not isinstance(author, dict)
                or set(author) != {"name", "email"}
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or value != value.strip()
                    or any(ord(char) < 32 or ord(char) == 127 or char in "<>" for char in value)
                    for value in author.values()
                )
            ):
                raise RuntimeError("GitHub machine commit_author requires a valid name and email")
            email = author["email"]
            if (
                email.count("@") != 1
                or not all(email.split("@"))
                or any(char.isspace() for char in email)
            ):
                raise RuntimeError("GitHub machine commit_author requires a valid email")
            commit_author = GitCommitAuthor(author["name"], email)
        result[principal] = GitHubMachineBinding(
            installation, tuple(sorted(repos)), access, ci_read, issues_access, commit_author
        )
    return result


class GitHubMachineBroker:
    """Cache short-lived tokens in memory, checking the live machine binding on every vend."""

    def __init__(
        self,
        bindings: dict[str, GitHubMachineBinding],
        client: GitHubAppClient,
        principal_allowed: Callable[[str], bool],
    ) -> None:
        self._bindings = dict(bindings)
        self._client = client
        self._principal_allowed = principal_allowed
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks = {principal: asyncio.Lock() for principal in bindings}

    async def resolve(self, principal: str) -> dict[str, object] | None:
        binding = self._bindings.get(principal)
        if binding is None or not await asyncio.to_thread(self._principal_allowed, principal):
            return None
        async with self._locks[principal]:
            if not await asyncio.to_thread(self._principal_allowed, principal):
                return None
            cached = self._tokens.get(principal)
            if cached is None or cached[1] <= time.time() + 300:
                token, expiry = await self._client.installation_token(
                    binding.installation_id,
                    repository_ids=binding.repository_ids,
                    permissions=binding.permissions,
                )
                # Revocation can happen while the GitHub request is in flight.
                if not await asyncio.to_thread(self._principal_allowed, principal):
                    return None
                cached = (token, expiry)
                self._tokens[principal] = cached
        credential: dict[str, object] = {
            "username": "x-access-token",
            "token": cached[0],
            "expires_at": cached[1],
            "login": self._client.bot_login,
        }
        if binding.commit_author is not None:
            credential["commit_author"] = {
                "name": binding.commit_author.name,
                "email": binding.commit_author.email,
            }
        return credential
