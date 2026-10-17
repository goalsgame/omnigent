"""SQL publication catalog; blob lifetime is independent of session files."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlPublishedArtifact, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)


@dataclass(frozen=True)
class PublishedArtifact:
    """One immutable publication and its mutable read grants."""

    id: str
    owner_id: str
    source_session_id: str
    title: str
    filename: str
    kind: str
    content_type: str
    byte_size: int
    sha256: str
    created_at: int
    expires_at: int
    deleted: bool = False
    grants_json: str = "[]"
    ready: bool = True

    def blob_key(self, workspace_id: int | None = None) -> str:
        """Return a namespace separate from session-file garbage collection."""
        workspace = current_workspace_id() if workspace_id is None else workspace_id
        return f"published/{workspace}/{self.id}"


def _entity(row: SqlPublishedArtifact) -> PublishedArtifact:
    return PublishedArtifact(
        **{field: getattr(row, field) for field in PublishedArtifact.__dataclass_fields__}
    )


class PublishedArtifactStore:
    """Store immutable metadata, tombstones and owner-controlled read grants."""

    def __init__(self, uri: str) -> None:
        engine = get_or_create_engine(uri)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.published_artifacts"
        )
        self._write_session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.published_artifacts", immediate=True
        )

    def create(self, artifact: PublishedArtifact) -> None:
        """Insert metadata after the immutable blob has been stored."""
        with self._write_session("create") as session:
            session.add(
                SqlPublishedArtifact(workspace_id=current_workspace_id(), **asdict(artifact))
            )

    def get(self, artifact_id: str) -> PublishedArtifact | None:
        """Return only a live publication in the current workspace."""
        with self._session("get") as session:
            row = session.get(SqlPublishedArtifact, (current_workspace_id(), artifact_id))
            if row is None or not row.ready or row.deleted or row.expires_at <= now_epoch():
                return None
            return _entity(row)

    def list_owned(self, owner_id: str, limit: int = 100) -> list[PublishedArtifact]:
        """List the owner's newest live publications."""
        with self._session("list_owned") as session:
            rows = session.scalars(
                select(SqlPublishedArtifact)
                .where(
                    SqlPublishedArtifact.workspace_id == current_workspace_id(),
                    SqlPublishedArtifact.owner_id == owner_id,
                    SqlPublishedArtifact.deleted.is_(False),
                    SqlPublishedArtifact.ready.is_(True),
                    SqlPublishedArtifact.expires_at > now_epoch(),
                )
                .order_by(SqlPublishedArtifact.created_at.desc(), SqlPublishedArtifact.id.desc())
                .limit(limit)
            )
            return [_entity(row) for row in rows]

    def mutate(
        self,
        artifact_id: str,
        owner_id: str,
        *,
        grants: list[dict[str, Any]] | None = None,
        delete: bool = False,
    ) -> bool:
        """Serialize owner-only grant replacement and immediate revocation."""

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlPublishedArtifact)
                .where(
                    SqlPublishedArtifact.workspace_id == current_workspace_id(),
                    SqlPublishedArtifact.id == artifact_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.owner_id != owner_id
                or row.deleted
                or row.expires_at <= now_epoch()
            ):
                return False
            if grants is not None:
                row.grants_json = json.dumps(grants)
            if delete:
                row.deleted = True
            return True

        return run_write_transaction(self._write_session, "mutate", write)

    def complete(self, artifact_id: str) -> bool:
        """Expose a pending publication only after its blob upload succeeds."""

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlPublishedArtifact)
                .where(
                    SqlPublishedArtifact.workspace_id == current_workspace_id(),
                    SqlPublishedArtifact.id == artifact_id,
                )
                .with_for_update()
            )
            if row is None or row.deleted or row.expires_at <= now_epoch():
                return False
            row.ready = True
            return True

        return run_write_transaction(self._write_session, "complete", write)

    def garbage(self, limit: int = 100) -> list[tuple[int, PublishedArtifact]]:
        """Enumerate expired/tombstoned rows across tenants for system cleanup."""
        with self._session("garbage") as session:
            rows = session.scalars(
                select(SqlPublishedArtifact)
                .where(
                    or_(
                        SqlPublishedArtifact.deleted.is_(True),
                        SqlPublishedArtifact.expires_at <= now_epoch(),
                        and_(
                            SqlPublishedArtifact.ready.is_(False),
                            SqlPublishedArtifact.created_at < now_epoch() - 3600,
                        ),
                    )
                )
                .limit(limit)
            )
            return [(row.workspace_id, _entity(row)) for row in rows]

    def claim_garbage(self, workspace_id: int, artifact_id: str) -> bool:
        """Tombstone under lock before cleanup so a late upload cannot become visible."""

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlPublishedArtifact)
                .where(
                    SqlPublishedArtifact.workspace_id == workspace_id,
                    SqlPublishedArtifact.id == artifact_id,
                )
                .with_for_update()
            )
            if row is None or not (
                row.deleted
                or row.expires_at <= now_epoch()
                or (not row.ready and row.created_at < now_epoch() - 3600)
            ):
                return False
            row.deleted = True
            return True

        return run_write_transaction(self._write_session, "claim_garbage", write)

    def forget(self, workspace_id: int, artifact_id: str) -> None:
        """Remove expired metadata only after idempotent blob cleanup succeeds."""
        with self._write_session("forget") as session:
            row = session.get(SqlPublishedArtifact, (workspace_id, artifact_id))
            if row is not None and (
                row.deleted
                or row.expires_at <= now_epoch()
                or (not row.ready and row.created_at < now_epoch() - 3600)
            ):
                session.delete(row)
