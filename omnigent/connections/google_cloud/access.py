"""Owner consent for credentials shared by a managed session's sandbox."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConnection,
    SqlConversationMetadata,
    SqlHost,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server.auth import RESERVED_USER_LOCAL


class GoogleCloudSessionAccess:
    def __init__(self, storage_location: str) -> None:
        self._session = make_named_managed_session_maker(
            get_or_create_engine(storage_location),
            query_name_prefix="omnigent.google_cloud_access",
            immediate=True,
        )

    def _context(self, db: Session, row: SqlConversationMetadata, user_id: str) -> dict[str, Any]:
        if user_id == RESERVED_USER_LOCAL:
            raise PermissionError("Google Cloud sandbox access requires server authentication")
        workspace = current_workspace_id()
        permission = db.get(SqlSessionPermission, (workspace, user_id, row.id))
        if permission is None or permission.level != 4 or user_id.startswith("oidc-machine:"):
            raise PermissionError("Only the human session owner can authorize Google Cloud")
        host = db.get(SqlHost, (workspace, row.host_id)) if row.host_id else None
        if (
            host is None
            or host.deleted_at is not None
            or host.user_id != user_id
            or not host.sandbox_provider
        ):
            raise ValueError("A managed sandbox is required")
        # Host tokens authorize the whole sandbox; ambiguous shared hosts fail closed.
        roots = db.scalars(
            select(SqlConversationMetadata.id).where(
                SqlConversationMetadata.workspace_id == workspace,
                SqlConversationMetadata.host_id == row.host_id,
                SqlConversationMetadata.kind == 1,
            )
        ).all()
        if roots != [row.id]:
            raise ValueError("Google access requires a sandbox dedicated to this session")
        connection = db.get(SqlConnection, (workspace, user_id, "google_cloud", ""))
        metadata = json.loads(connection.metadata_json) if connection else {}
        return {
            "user_id": user_id,
            "host_id": row.host_id,
            "generation": metadata.get("generation"),
            "email": metadata.get("email"),
        }

    @staticmethod
    def _state(row: SqlConversationMetadata, context: dict[str, Any]) -> str:
        if not context["generation"]:
            return "not_connected"
        try:
            saved = json.loads(row.google_cloud_access or "{}")
        except (ValueError, TypeError):
            return "off"
        if not isinstance(saved, dict) or any(
            saved.get(k) != context[k] for k in ("user_id", "host_id", "generation")
        ):
            return "off"
        state = saved.get("state")
        return state if state in ("allowed", "pending", "denied") else "off"

    def session(
        self,
        session_id: str,
        user_id: str,
        *,
        decision: str | None = None,
        generation: str | None = None,
        expected_host: str | None = None,
    ) -> dict[str, Any]:
        with self._session("session_access") as db:
            row = db.scalar(
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == session_id,
                )
                .with_for_update()
            )
            if row is None:
                raise PermissionError("Session not found")
            context = self._context(db, row, user_id)
            if expected_host is not None and (
                context["host_id"] != expected_host or context["generation"] != generation
            ):
                raise ValueError("Google Cloud access request is no longer current")
            if decision is not None:
                if decision not in ("allowed", "denied"):
                    raise ValueError("Unknown consent decision")
                if not generation or generation != context["generation"]:
                    raise ValueError("Google account changed; refresh before deciding")
                if expected_host is not None and self._state(row, context) != "pending":
                    raise ValueError("Google Cloud access request is no longer current")
                row.google_cloud_access = json.dumps({**context, "state": decision})
            return {
                "state": self._state(row, context),
                "generation": context["generation"],
                "email": context["email"],
            }

    def host(
        self,
        host_id: str,
        user_id: str,
        *,
        request_access: bool = False,
        expected_generation: str | None = None,
    ) -> str:
        with self._session("host_access") as db:
            rows = db.scalars(
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == host_id,
                    SqlConversationMetadata.kind == 1,
                )
                .with_for_update()
            ).all()
            if len(rows) != 1:
                return "unavailable"
            row = rows[0]
            try:
                context = self._context(db, row, user_id)
            except (PermissionError, ValueError):
                return "unavailable"
            if expected_generation is not None and context["generation"] != expected_generation:
                return "off"
            state = self._state(row, context)
            if request_access and state == "off":
                row.google_cloud_access = json.dumps({**context, "state": "pending"})
                return "pending"
            return state

    def host_request(self, host_id: str) -> dict[str, Any] | None:
        """Resolve an unambiguous managed sandbox to its human owner's consent."""
        with self._session("host_request") as db:
            host = db.get(SqlHost, (current_workspace_id(), host_id))
            if host is None:
                return None
            rows = db.scalars(
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.host_id == host_id,
                    SqlConversationMetadata.kind == 1,
                )
                .with_for_update()
            ).all()
            if len(rows) != 1:
                return None
            row = rows[0]
            try:
                context = self._context(db, row, host.user_id)
            except (PermissionError, ValueError):
                return None
            state = self._state(row, context)
            if state == "off":
                state = "pending"
                row.google_cloud_access = json.dumps({**context, "state": state})
            return {**context, "session_id": row.id, "state": state}
