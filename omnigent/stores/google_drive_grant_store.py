"""Per-session Google Drive grants."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlGoogleDriveGrant, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

_T = TypeVar("_T")
DRIVE_SCOPE_KEY = "*"


@dataclass(frozen=True)
class GoogleDriveGrant:
    """One person's approval for a session's agents to use a file or their whole Drive."""

    id: str
    session_id: str
    user_id: str
    connection_generation: str
    kind: str
    access: str
    created_at: int
    audience: dict[str, int] = field(default_factory=dict)
    file_id: str | None = None
    name: str | None = None
    mime_type: str | None = None

    @property
    def scope_key(self) -> str:
        return self.file_id if self.kind == "file" and self.file_id else DRIVE_SCOPE_KEY


def _entity(row: SqlGoogleDriveGrant) -> GoogleDriveGrant:
    return GoogleDriveGrant(
        id=row.id,
        session_id=row.session_id,
        user_id=row.user_id,
        connection_generation=row.connection_generation,
        kind=row.kind,
        access=row.access,
        created_at=row.created_at,
        audience=json.loads(row.audience),
        file_id=row.file_id,
        name=row.name,
        mime_type=row.mime_type,
    )


class GoogleDriveGrantStore:
    def __init__(self, uri: str) -> None:
        engine = get_or_create_engine(uri)
        self._read = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.google_drive_grants"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.google_drive_grants", immediate=True
        )

    @staticmethod
    def _scope(session_id: str):
        return (
            SqlGoogleDriveGrant.workspace_id == current_workspace_id(),
            SqlGoogleDriveGrant.session_id == session_id,
        )

    def _write_unique(self, operation: str, callback: Callable[[Session], _T]) -> _T:
        """Retry when a concurrent writer inserted the same person's grant for the same scope."""
        for attempt in range(3):
            try:
                return run_write_transaction(self._write, operation, callback)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def list(self, session_id: str) -> list[GoogleDriveGrant]:
        with self._read("list") as session:
            rows = session.scalars(
                select(SqlGoogleDriveGrant)
                .where(*self._scope(session_id))
                .order_by(SqlGoogleDriveGrant.created_at, SqlGoogleDriveGrant.id)
            ).all()
            return [_entity(row) for row in rows]

    @classmethod
    def _upsert(cls, session: Session, grant: GoogleDriveGrant) -> GoogleDriveGrant:
        row = session.scalar(
            select(SqlGoogleDriveGrant)
            .where(
                *cls._scope(grant.session_id),
                SqlGoogleDriveGrant.user_id == grant.user_id,
                SqlGoogleDriveGrant.scope_key == grant.scope_key,
            )
            .with_for_update()
        )
        if row is None:
            row = SqlGoogleDriveGrant(
                workspace_id=current_workspace_id(),
                id=grant.id,
                session_id=grant.session_id,
                user_id=grant.user_id,
                scope_key=grant.scope_key,
            )
            session.add(row)
        row.connection_generation = grant.connection_generation
        row.kind = grant.kind
        row.file_id = grant.file_id
        row.name = grant.name
        row.mime_type = grant.mime_type
        row.access = grant.access
        row.audience = json.dumps(grant.audience, sort_keys=True)
        row.created_at = grant.created_at
        session.flush()
        return _entity(row)

    def put(self, grant: GoogleDriveGrant) -> GoogleDriveGrant:
        """Store *grant*, replacing the same person's grant for the same file or Drive."""
        return self._write_unique("put", lambda session: self._upsert(session, grant))

    def derive(self, source: GoogleDriveGrant, grant: GoogleDriveGrant) -> GoogleDriveGrant | None:
        """Store *grant* only while *source* still exists unchanged in the same transaction."""

        def write(session: Session) -> GoogleDriveGrant | None:
            row = session.scalar(
                select(SqlGoogleDriveGrant)
                .where(*self._scope(source.session_id), SqlGoogleDriveGrant.id == source.id)
                .with_for_update()
            )
            if row is None or _entity(row) != source:
                return None
            return self._upsert(session, grant)

        return self._write_unique("derive", write)

    def acknowledge(
        self, session_id: str, grant_id: str, user_id: str, audience: dict[str, int]
    ) -> GoogleDriveGrant | None:
        def write(session: Session) -> GoogleDriveGrant | None:
            row = session.scalar(
                select(SqlGoogleDriveGrant)
                .where(
                    *self._scope(session_id),
                    SqlGoogleDriveGrant.id == grant_id,
                    SqlGoogleDriveGrant.user_id == user_id,
                )
                .with_for_update()
            )
            if row is None:
                return None
            row.audience = json.dumps(audience, sort_keys=True)
            return _entity(row)

        return run_write_transaction(self._write, "acknowledge", write)

    def delete(self, session_id: str, grant_id: str) -> bool:
        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlGoogleDriveGrant)
                .where(*self._scope(session_id), SqlGoogleDriveGrant.id == grant_id)
                .with_for_update()
            )
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._write, "delete", write)
