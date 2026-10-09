"""Operator-authorized Google service accounts for machine-owned sandboxes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlHost,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker


@dataclass(frozen=True)
class MachineCloudBinding:
    service_account: str

    @property
    def generation(self) -> str:
        return hashlib.sha256(self.service_account.encode()).hexdigest()


def parse_machine_cloud_bindings(
    value: object, *, principals: frozenset[str]
) -> dict[str, MachineCloudBinding]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError("google_cloud_machine_auth must be a machine binding mapping")
    result = {}
    for principal, raw in value.items():
        if (
            principal not in principals
            or not isinstance(raw, dict)
            or set(raw) != {"service_account"}
        ):
            raise ValueError(
                "Google Cloud bindings require a configured machine and service_account"
            )
        account = raw["service_account"]
        if not isinstance(account, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com",
            account,
        ):
            raise ValueError("Invalid Google Cloud machine service account")
        result[principal] = MachineCloudBinding(account)
    return result


class GoogleCloudMachineBroker:
    """Bind opt-in to the root session and current operator policy; vend ten-minute tokens."""

    def __init__(
        self,
        storage_location: str,
        bindings: dict[str, MachineCloudBinding],
        principal_allowed: Callable[[str], bool],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.bindings = dict(bindings)
        self.principal_allowed = principal_allowed
        self.transport = transport
        self._session = make_named_managed_session_maker(
            get_or_create_engine(storage_location),
            query_name_prefix="omnigent.machine_cloud",
            immediate=True,
        )
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._locks = {p: asyncio.Lock() for p in bindings}

    def binding(self, principal: str) -> MachineCloudBinding:
        binding = self.bindings.get(principal)
        if binding is None or not self.principal_allowed(principal):
            raise PermissionError("Machine Google Cloud access is not configured")
        return binding

    def session(
        self,
        session_id: str,
        principal: str,
        *,
        enabled: bool | None = None,
        expected_host: str | None = None,
    ) -> dict[str, Any]:
        binding = self.binding(principal)
        with self._session("session_access") as db:
            row = db.scalar(
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == session_id,
                )
                .with_for_update()
            )
            owner = db.get(SqlSessionPermission, (current_workspace_id(), principal, session_id))
            if row is None or row.kind != 1 or owner is None or owner.level != 4:
                raise PermissionError("Machine session owner required")
            if expected_host is not None and row.host_id != expected_host:
                raise PermissionError("Machine sandbox assignment changed")
            owners = db.scalars(
                select(SqlSessionPermission.user_id).where(
                    SqlSessionPermission.workspace_id == current_workspace_id(),
                    SqlSessionPermission.conversation_id == session_id,
                    SqlSessionPermission.level == 4,
                )
            ).all()
            if owners != [principal]:
                raise PermissionError("Unique machine session owner required")
            if enabled is not None:
                row.google_cloud_access = json.dumps(
                    {
                        "kind": "machine",
                        "user_id": principal,
                        "generation": binding.generation,
                        "state": "allowed" if enabled else "off",
                    }
                )
            try:
                saved = json.loads(row.google_cloud_access or "{}")
            except (ValueError, TypeError):
                saved = {}
            allowed = isinstance(saved, dict) and all(
                saved.get(k) == v
                for k, v in {
                    "kind": "machine",
                    "user_id": principal,
                    "generation": binding.generation,
                    "state": "allowed",
                }.items()
            )
            return {
                "state": "allowed" if allowed else "off",
                "authorization": "operator",
                "email": binding.service_account,
                "generation": binding.generation,
                "owner": principal,
            }

    def host(self, host_id: str, principal: str | None = None) -> dict[str, Any] | None:
        with self._session("host_access") as db:
            host = db.get(SqlHost, (current_workspace_id(), host_id))
            if host is None or not host.user_id.startswith("oidc-machine:"):
                return None
            if (
                host.deleted_at is not None
                or not host.sandbox_provider
                or (principal is not None and host.user_id != principal)
            ):
                raise PermissionError("Machine sandbox is unavailable")
            roots = db.scalars(
                select(SqlConversationMetadata.id).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == host_id,
                    SqlConversationMetadata.kind == 1,
                )
            ).all()
            if len(roots) != 1:
                raise PermissionError("Machine Google Cloud access requires a dedicated sandbox")
            session_id, owner = roots[0], host.user_id
        return {
            **self.session(session_id, owner, expected_host=host_id),
            "session_id": session_id,
            "host_id": host_id,
        }

    async def credential(self, host_id: str, principal: str) -> dict[str, Any]:
        context = await asyncio.to_thread(self.host, host_id, principal)
        if context is None or context["state"] != "allowed":
            raise PermissionError("Machine Google Cloud access was not requested for this session")
        async with self._locks[principal]:
            cache_key = (principal, context["generation"])
            cached = self._tokens.get(cache_key)
            if cached is None or cached[1] <= time.time() + 60:
                cached = await self._mint(context["email"])
                self._tokens[cache_key] = cached
        current = await asyncio.to_thread(self.host, host_id, principal)
        if current != context:
            raise PermissionError("Machine Google Cloud authorization changed")
        return {
            "connected": True,
            "owner": principal,
            "token": cached[0],
            "expires_at": cached[1],
            "email": context["email"],
        }

    async def _mint(self, account: str) -> tuple[str, float]:
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False, transport=self.transport
        ) as client:
            response = await client.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"},
            )
            response.raise_for_status()
            source = response.json()["access_token"]
            response = await client.post(
                f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{account}:generateAccessToken",
                headers={"Authorization": f"Bearer {source}"},
                json={
                    "scope": ["https://www.googleapis.com/auth/cloud-platform"],
                    "lifetime": "600s",
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = payload["accessToken"]
            expiry = datetime.fromisoformat(
                payload["expireTime"].replace("Z", "+00:00")
            ).timestamp()
            if (
                not isinstance(token, str)
                or not token
                or any(c.isspace() for c in token)
                or not time.time() + 60 < expiry <= time.time() + 660
            ):
                raise ValueError("Invalid machine credential response")
            return token, expiry
