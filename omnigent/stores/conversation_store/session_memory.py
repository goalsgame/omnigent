"""Storage for session digests and their embedded, searchable entries."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    String,
    and_,
    bindparam,
    cast,
    delete,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session
from sqlalchemy.types import UserDefinedType

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationItem,
    SqlSessionDigest,
    SqlSessionMemoryEntry,
    Uuid16,
    current_workspace_id,
)
from omnigent.db.enum_codecs import ITEM_TYPE
from omnigent.db.utils import run_write_transaction

ENTRY_KINDS = ("summary", "decision", "discussion", "open_question", "tangent")
TRANSCRIPT_ITEM_TYPES = ("message", "compaction")
MAX_ITEM_CHARS = 8_000


class Vector(UserDefinedType[list[float]]):
    """A pgvector value, bound as its ``[x,y,…]`` text form."""

    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "vector"


def vector_literal(values: Sequence[float]) -> str:
    """The pgvector text form of *values*."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def embedding_column() -> Any:
    return literal_column("session_memory_entries.embedding", type_=Vector())


def query_vector(values: Sequence[float]) -> Any:
    return cast(bindparam("query_embedding", vector_literal(values), type_=String()), Vector())


@dataclass(frozen=True)
class TranscriptItem:
    """A message or compaction summary in a top-level session."""

    item_id: str
    position: int
    created_at: int
    role: str
    text: str
    response_id: str = ""

    @property
    def message_id(self) -> str:
        """The id the web transcript deep-links to: user item ids, assistant response ids."""
        return self.response_id if self.role == "assistant" else self.item_id


@dataclass(frozen=True)
class MemoryEntry:
    """One searchable statement from a digest, citing the messages it came from."""

    kind: str
    text: str
    item_ids: list[str] = field(default_factory=list)
    created_at: int = 0


@dataclass(frozen=True)
class SessionDigestRecord:
    """The stored digest state for one top-level session."""

    session_id: str
    status: str
    digest: dict[str, Any] | None
    error: str | None
    model: str | None
    items_through: int
    source_updated_at: int
    attempts: int
    updated_at: int


def _record(row: SqlSessionDigest) -> SessionDigestRecord:
    return SessionDigestRecord(
        session_id=row.conversation_id,
        status=row.status,
        digest=json.loads(row.digest) if row.digest else None,
        error=row.error,
        model=row.model,
        items_through=row.items_through,
        source_updated_at=row.source_updated_at,
        attempts=row.attempts,
        updated_at=row.updated_at or 0,
    )


def _role(item_type: int, data: str) -> str:
    if item_type == ITEM_TYPE["compaction"]:
        return "summary"
    try:
        role = json.loads(data).get("role")
    except (ValueError, AttributeError):
        return "message"
    return role if isinstance(role, str) and role else "message"


class SessionMemoryStore:
    """Session memory operations for a SQLAlchemy conversation store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _write(self, name: str, callback: Any) -> Any:
        return run_write_transaction(self._store._conv_session_immediate, name, callback)

    def candidates(self, now: int, idle_seconds: int, limit: int) -> list[tuple[int, str]]:
        """
        Quiet top-level sessions whose digest is missing or older than the session.

        Spans every workspace; callers process each row inside its
        ``workspace_scope``. Most recently active sessions come first.
        """
        root = SqlConversation
        digest = SqlSessionDigest
        with self._store._conv_session("session_memory_candidates") as session:
            rows = session.execute(
                select(root.workspace_id, root.id)
                .outerjoin(
                    digest,
                    and_(
                        digest.workspace_id == root.workspace_id,
                        digest.conversation_id == root.id,
                    ),
                )
                .where(
                    root.parent_conversation_id.is_(None),
                    root.archived.is_(False),
                    root.updated_at <= now - idle_seconds,
                    or_(
                        digest.conversation_id.is_(None),
                        and_(
                            digest.source_updated_at < root.updated_at,
                            digest.next_attempt_at <= now,
                            digest.lease_until <= now,
                        ),
                    ),
                )
                .order_by(root.updated_at.desc())
                .limit(limit)
            ).all()
        return [(int(workspace), str(root_id)) for workspace, root_id in rows]

    def claim(self, session_id: str, now: int, lease_seconds: int) -> SessionDigestRecord | None:
        """Lease *session_id* for one worker, creating its digest row if needed."""

        def claim(session: Session) -> SessionDigestRecord | None:
            workspace = current_workspace_id()
            row = session.get(SqlSessionDigest, (workspace, session_id), with_for_update=True)
            if row is None:
                row = SqlSessionDigest(
                    workspace_id=workspace,
                    conversation_id=session_id,
                    status="pending",
                    source_updated_at=0,
                    items_through=-1,
                    attempts=0,
                    next_attempt_at=0,
                    lease_until=now + lease_seconds,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return _record(row)
            if row.lease_until > now or row.next_attempt_at > now:
                return None
            row.lease_until = now + lease_seconds
            return _record(row)

        return self._write("session_memory_claim", claim)

    def session_state(self, session_id: str) -> tuple[str, int] | None:
        """The title and ``updated_at`` of a live top-level session."""
        with self._store._conv_session("session_memory_state") as session:
            row = session.execute(
                select(SqlConversation.title, SqlConversation.updated_at).where(
                    SqlConversation.workspace_id == current_workspace_id(),
                    SqlConversation.id == session_id,
                    SqlConversation.parent_conversation_id.is_(None),
                )
            ).first()
        return None if row is None else (row[0] or "", int(row[1]))

    def transcript(
        self, session_id: str, after_position: int, max_chars: int
    ) -> tuple[list[TranscriptItem], bool]:
        """
        Messages after *after_position*, oldest first, up to *max_chars* of text.

        :returns: The items and whether more remain beyond the budget.
        """
        items: list[TranscriptItem] = []
        used = 0
        with self._store._conv_session("session_memory_transcript") as session:
            rows = session.execute(
                select(
                    SqlConversationItem.id,
                    SqlConversationItem.position,
                    SqlConversationItem.created_at,
                    SqlConversationItem.type,
                    SqlConversationItem.data,
                    SqlConversationItem.search_text,
                    SqlConversationItem.response_id,
                )
                .where(
                    SqlConversationItem.workspace_id == current_workspace_id(),
                    SqlConversationItem.conversation_id == session_id,
                    SqlConversationItem.position > after_position,
                    SqlConversationItem.type.in_(
                        [ITEM_TYPE[name] for name in TRANSCRIPT_ITEM_TYPES]
                    ),
                )
                .order_by(SqlConversationItem.position)
            )
            for item_id, position, created_at, item_type, data, body, response_id in rows:
                content = " ".join((body or "").split())
                if not content:
                    continue
                if len(content) > MAX_ITEM_CHARS:
                    content = content[:MAX_ITEM_CHARS] + "…"
                if items and used + len(content) > max_chars:
                    return items, True
                used += len(content)
                items.append(
                    TranscriptItem(
                        item_id=item_id,
                        position=position,
                        created_at=created_at,
                        role=_role(item_type, data),
                        text=content,
                        response_id=response_id,
                    )
                )
        return items, False

    def save(
        self,
        session_id: str,
        *,
        digest: dict[str, Any],
        entries: Sequence[MemoryEntry],
        items_through: int,
        source_updated_at: int,
        model: str,
        now: int,
    ) -> None:
        """Replace the digest and its entries and release the lease, unless the session is gone."""

        def save(session: Session) -> None:
            workspace = current_workspace_id()
            claimed = session.get(SqlSessionDigest, (workspace, session_id), with_for_update=True)
            if claimed is None or session.get(SqlConversation, (workspace, session_id)) is None:
                return
            session.execute(
                delete(SqlSessionMemoryEntry).where(
                    SqlSessionMemoryEntry.workspace_id == workspace,
                    SqlSessionMemoryEntry.conversation_id == session_id,
                )
            )
            session.add_all(
                SqlSessionMemoryEntry(
                    workspace_id=workspace,
                    id=uuid.uuid4().hex,
                    conversation_id=session_id,
                    kind=entry.kind,
                    ordinal=ordinal,
                    text=entry.text,
                    item_ids=json.dumps(entry.item_ids),
                    created_at=entry.created_at or now,
                )
                for ordinal, entry in enumerate(entries)
            )
            session.execute(
                update(SqlSessionDigest)
                .where(
                    SqlSessionDigest.workspace_id == workspace,
                    SqlSessionDigest.conversation_id == session_id,
                )
                .values(
                    digest=json.dumps(digest, separators=(",", ":")),
                    status="ready",
                    error=None,
                    attempts=0,
                    next_attempt_at=0,
                    lease_until=0,
                    items_through=items_through,
                    source_updated_at=source_updated_at,
                    model=model,
                    updated_at=now,
                )
            )

        self._write("session_memory_save", save)

    def advance(
        self, session_id: str, *, items_through: int, source_updated_at: int, now: int
    ) -> None:
        """Record that nothing new needs digesting and release the lease."""

        def advance(session: Session) -> None:
            row = session.get(SqlSessionDigest, (current_workspace_id(), session_id))
            if row is None:
                return
            row.items_through = items_through
            row.source_updated_at = source_updated_at
            row.lease_until = 0
            if row.status == "pending":
                row.status = "empty"
            row.updated_at = now

        self._write("session_memory_advance", advance)

    def fail(self, session_id: str, error: str, *, now: int, retry_at: int) -> None:
        """Record a failed attempt and when to retry it."""

        def fail(session: Session) -> None:
            row = session.get(SqlSessionDigest, (current_workspace_id(), session_id))
            if row is None:
                return
            row.attempts += 1
            row.error = error[:512]
            row.next_attempt_at = retry_at
            row.lease_until = 0
            if row.digest is None:
                row.status = "failed"
            row.updated_at = now

        self._write("session_memory_fail", fail)

    def get(self, session_id: str) -> SessionDigestRecord | None:
        """The digest state for *session_id*, or ``None`` when never processed."""
        with self._store._conv_session("session_memory_get") as session:
            row = session.get(SqlSessionDigest, (current_workspace_id(), session_id))
            return None if row is None else _record(row)

    def unembedded(self, limit: int) -> list[tuple[int, str, str]]:
        """Entries in any workspace still waiting for an embedding."""
        with self._store._conv_session("session_memory_unembedded") as session:
            rows = session.execute(
                select(
                    SqlSessionMemoryEntry.workspace_id,
                    SqlSessionMemoryEntry.id,
                    SqlSessionMemoryEntry.text,
                )
                .where(embedding_column().is_(None))
                .limit(limit)
            ).all()
        return [(int(workspace), str(entry_id), str(body)) for workspace, entry_id, body in rows]

    def set_embedding(self, workspace: int, entry_id: str, values: Sequence[float]) -> None:
        """Store the embedding for one entry."""

        def store(session: Session) -> None:
            session.execute(
                text(
                    "UPDATE session_memory_entries SET embedding = CAST(:embedding AS vector) "
                    "WHERE workspace_id = :workspace AND id = :entry_id"
                ).bindparams(bindparam("entry_id", type_=Uuid16())),
                {
                    "embedding": vector_literal(values),
                    "workspace": workspace,
                    "entry_id": entry_id,
                },
            )

        self._write("session_memory_set_embedding", store)

    def delete_for(self, session: Session, session_ids: Sequence[str]) -> None:
        """
        Delete digests and entries for *session_ids* inside the caller's transaction.

        Digests must be deleted before entries; an in-flight :meth:`save` holds the digest row.
        """
        workspace = current_workspace_id()
        session.execute(
            delete(SqlSessionDigest).where(
                SqlSessionDigest.workspace_id == workspace,
                SqlSessionDigest.conversation_id.in_(session_ids),
            )
        )
        session.execute(
            delete(SqlSessionMemoryEntry).where(
                SqlSessionMemoryEntry.workspace_id == workspace,
                SqlSessionMemoryEntry.conversation_id.in_(session_ids),
            )
        )
