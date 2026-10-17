"""Durable session inboxes, independent of runner or backend process lifetime."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_generation, require_active_account
from omnigent.db.db_models import SqlPeerMessage, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError


@dataclass(frozen=True)
class PeerMessage:
    """Immutable sender attribution and content with shared recipient acknowledgement."""

    id: str
    sender_session_id: str
    recipient_session_id: str
    sender_user_id: str
    subject: str
    body: str
    created_at: int
    expires_at: int
    reply_to: str | None = None
    acknowledged_at: int | None = None
    sender_generation: str | None = None


def _entity(row: SqlPeerMessage) -> PeerMessage:
    return PeerMessage(**{key: getattr(row, key) for key in PeerMessage.__dataclass_fields__})


class PeerMessageStore:
    """Persist messages and idempotent acknowledgements within one workspace."""

    def __init__(self, uri: str) -> None:
        engine = get_or_create_engine(uri)
        self._read = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.peer_messages"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.peer_messages", immediate=True
        )

    def send(self, message: PeerMessage) -> PeerMessage:
        """Insert once; a repeated message id must describe the same authenticated send."""

        def write(session: Session) -> PeerMessage:
            require_active_account(
                session,
                message.sender_user_id,
                generation=account_generation(message.sender_user_id),
            )
            session.execute(
                delete(SqlPeerMessage).where(
                    SqlPeerMessage.workspace_id == current_workspace_id(),
                    SqlPeerMessage.expires_at <= now_epoch_us(),
                )
            )
            old = session.get(SqlPeerMessage, (current_workspace_id(), message.id))
            if old is not None:
                existing = _entity(old)
                if any(
                    getattr(existing, key) != getattr(message, key)
                    for key in (
                        "sender_session_id",
                        "recipient_session_id",
                        "sender_user_id",
                        "sender_generation",
                        "subject",
                        "body",
                        "reply_to",
                    )
                ):
                    raise OmnigentError("Message id already used", code=ErrorCode.CONFLICT)
                return existing
            if message.reply_to:
                parent = session.get(SqlPeerMessage, (current_workspace_id(), message.reply_to))
                if parent is None or {parent.sender_session_id, parent.recipient_session_id} != {
                    message.sender_session_id,
                    message.recipient_session_id,
                }:
                    raise OmnigentError("Reply target not found", code=ErrorCode.NOT_FOUND)
            session.add(SqlPeerMessage(workspace_id=current_workspace_id(), **asdict(message)))
            return message

        try:
            return run_write_transaction(self._write, "send", write)
        except IntegrityError:
            # Another backend can commit the same id between lookup and insert.
            return run_write_transaction(self._write, "send_retry", write)

    def list_messages(
        self,
        session_id: str,
        *,
        sent: bool = False,
        unread_only: bool = False,
        after: tuple[int, str] | None = None,
        limit: int = 20,
    ) -> list[PeerMessage]:
        """Read a bounded chronological page; reading never acknowledges delivery."""
        with self._read("read_inbox" if not sent else "read_outbox") as session:
            query = select(SqlPeerMessage).where(
                SqlPeerMessage.workspace_id == current_workspace_id(),
                (SqlPeerMessage.sender_session_id if sent else SqlPeerMessage.recipient_session_id)
                == session_id,
                SqlPeerMessage.expires_at > now_epoch_us(),
            )
            if unread_only:
                query = query.where(SqlPeerMessage.acknowledged_at.is_(None))
            if after:
                query = query.where(
                    or_(
                        SqlPeerMessage.created_at > after[0],
                        and_(SqlPeerMessage.created_at == after[0], SqlPeerMessage.id > after[1]),
                    )
                )
            return [
                _entity(row)
                for row in session.scalars(
                    query.order_by(SqlPeerMessage.created_at, SqlPeerMessage.id).limit(limit)
                )
            ]

    def acknowledge(self, session_id: str, message_id: str, actor: str) -> bool:
        """Mark a recipient's message acknowledged without losing concurrent new messages."""

        def write(session: Session) -> bool:
            require_active_account(session, actor, generation=account_generation(actor))
            row = session.get(SqlPeerMessage, (current_workspace_id(), message_id))
            if (
                row is None
                or row.recipient_session_id != session_id
                or row.expires_at <= now_epoch_us()
            ):
                return False
            session.execute(
                update(SqlPeerMessage)
                .where(
                    SqlPeerMessage.workspace_id == current_workspace_id(),
                    SqlPeerMessage.id == message_id,
                    SqlPeerMessage.acknowledged_at.is_(None),
                )
                .values(acknowledged_at=now_epoch_us())
            )
            return True

        return run_write_transaction(self._write, "acknowledge", write)
