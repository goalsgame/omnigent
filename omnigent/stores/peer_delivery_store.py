"""Leased, batched notifications independent of message acknowledgement."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_generation, require_active_account
from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlPeerDelivery,
    SqlPeerMessage,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.utils import now_epoch_us, run_write_transaction
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.peer_message_store import PeerMessageStore


@dataclass(frozen=True)
class DeliveryPolicy:
    """Owner-controlled automatic processing and optional managed-sandbox wake."""

    enabled: bool = True
    wake_on_message: bool = False

    @classmethod
    def parse(cls, value: object) -> DeliveryPolicy:
        """Reject invalid declarative defaults instead of enabling accidental wake."""
        if value is None:
            return cls()
        if (
            not isinstance(value, dict)
            or set(value) - {"enabled", "wake_on_message"}
            or any(type(v) is not bool for v in value.values())
        ):
            raise ValueError("peer_messages requires boolean enabled and wake_on_message settings")
        return cls(**value)


@dataclass(frozen=True)
class PeerDelivery:
    """One bounded batch, retried with a stable id until accepted by the runner."""

    session_id: str
    mailbox_id: str
    lease: str
    id: str
    message_ids: tuple[str, ...]
    cursor: int


class PeerDeliveryStore(PeerMessageStore):
    """Serialize deliveries per mailbox; limit automatic processing to six batches/hour."""

    def candidates(
        self, *, limit: int = 100, after: tuple[int, str] | None = None
    ) -> list[tuple[int, str, str]]:
        """Scan eligible mailboxes across workspaces without reading message bodies."""
        now = now_epoch_us()
        m, d, c = SqlPeerMessage, SqlPeerDelivery, SqlConversationMetadata
        with self._read("notification_candidates") as session:
            rows = session.execute(
                select(m.workspace_id, m.recipient_session_id, m.recipient_mailbox_id)
                .join(
                    c,
                    and_(
                        c.workspace_id == m.workspace_id,
                        c.id == m.recipient_session_id,
                        c.peer_mailbox_id == m.recipient_mailbox_id,
                    ),
                )
                .outerjoin(
                    d,
                    and_(d.workspace_id == m.workspace_id, d.mailbox_id == m.recipient_mailbox_id),
                )
                .where(
                    m.acknowledged_at.is_(None),
                    m.expires_at > now,
                    or_(
                        m.workspace_id > after[0],
                        and_(m.workspace_id == after[0], m.recipient_session_id > after[1]),
                    )
                    if after
                    else true(),
                    or_(d.mailbox_id.is_(None), and_(d.cursor < m.created_at, d.retry_at <= now)),
                )
                .distinct()
                .order_by(m.workspace_id, m.recipient_session_id)
                .limit(limit)
            ).all()
            return [(row[0], row[1], row[2]) for row in rows]

    def _owner(self, session: Session, session_id: str) -> list[str | None] | None:
        row = session.execute(
            select(SqlSessionPermission.user_id, SqlUser.account_generation)
            .outerjoin(
                SqlUser,
                and_(
                    SqlUser.workspace_id == SqlSessionPermission.workspace_id,
                    SqlUser.id == SqlSessionPermission.user_id,
                ),
            )
            .where(
                SqlSessionPermission.workspace_id == current_workspace_id(),
                SqlSessionPermission.conversation_id == session_id,
                SqlSessionPermission.level >= 4,
                SqlSessionPermission.is_group.is_(False),
                SqlUser.deleted_at.is_(None),
            )
            .order_by(SqlSessionPermission.level.desc())
            .limit(1)
        ).first()
        return [row[0], row[1]] if row else None

    def _policy(
        self, session: Session, row: SqlPeerDelivery, default: DeliveryPolicy
    ) -> DeliveryPolicy:
        if not row.settings:
            return default
        saved = json.loads(row.settings)
        return (
            DeliveryPolicy.parse(saved["policy"])
            if saved["owner"] == self._owner(session, row.session_id)
            else default
        )

    def policy(self, session_id: str, mailbox_id: str, default: DeliveryPolicy) -> DeliveryPolicy:
        """Read an explicit override or inherit the current agent defaults."""
        with self._read("notification_policy") as session:
            row = session.get(SqlPeerDelivery, (current_workspace_id(), mailbox_id))
            return (
                self._policy(session, row, default)
                if row and row.session_id == session_id
                else default
            )

    def _state(self, session: Session, session_id: str, mailbox_id: str) -> SqlPeerDelivery:
        current = session.scalar(
            select(SqlConversationMetadata)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == session_id,
            )
            .with_for_update()
        )
        if current is None or current.peer_mailbox_id != mailbox_id:
            raise OmnigentError("Session changed", code=ErrorCode.CONFLICT)
        row = session.get(SqlPeerDelivery, (current_workspace_id(), mailbox_id))
        if row is None:
            row = SqlPeerDelivery(
                workspace_id=current_workspace_id(),
                mailbox_id=mailbox_id,
                session_id=session_id,
                cursor=0,
                retry_at=0,
                window_start=0,
                attempts=0,
            )
            session.add(row)
        return row

    def set_policy(
        self, session_id: str, mailbox_id: str, actor: str, policy: DeliveryPolicy
    ) -> None:
        """Update policy while checking account and mailbox incarnation atomically."""

        def write(session: Session) -> None:
            require_active_account(session, actor, generation=account_generation(actor))
            row = self._state(session, session_id, mailbox_id)
            owner = self._owner(session, session_id)
            if owner is None or owner[0] != actor:
                raise OmnigentError(
                    "Only the current owner can configure notifications", code=ErrorCode.FORBIDDEN
                )
            row.settings = json.dumps(
                {
                    "owner": owner,
                    "policy": {
                        "enabled": policy.enabled,
                        "wake_on_message": policy.wake_on_message,
                    },
                }
            )
            if row.lease is None:
                row.retry_at = 0

        run_write_transaction(self._write, "set_notification_policy", write)

    def begin_owner_turn(
        self,
        session_id: str,
        mailbox_id: str,
        actor: str,
        notification_id: str,
        default: DeliveryPolicy,
    ) -> None:
        """Attribute a new native turn only to the current authenticated recipient owner."""
        from omnigent.stores.conversation_store.sqlalchemy_store import _upsert_labels

        def write(session: Session) -> None:
            require_active_account(session, actor, generation=account_generation(actor))
            row = self._state(session, session_id, mailbox_id)
            owner = self._owner(session, session_id)
            if (
                owner is None
                or owner[0] != actor
                or not self._policy(session, row, default).enabled
            ):
                raise OmnigentError(
                    "Automatic peer turn is not authorized", code=ErrorCode.FORBIDDEN
                )
            if (
                row.pending_ids is None
                or hashlib.sha256(row.pending_ids.encode()).hexdigest() != notification_id
            ):
                raise OmnigentError(
                    "Peer notification is no longer pending", code=ErrorCode.CONFLICT
                )
            _upsert_labels(
                session, session_id, {"omnigent.turn_actor": actor}, now_epoch_us() // 1_000_000
            )

        run_write_transaction(self._write, "begin_notification_turn", write)

    def defer(self, session_id: str, mailbox_id: str, *, seconds: int = 30) -> None:
        """Back off unavailable sessions without consuming automatic-turn budget."""

        def write(session: Session) -> None:
            row = self._state(session, session_id, mailbox_id)
            if row.lease is None:
                row.retry_at = max(row.retry_at, now_epoch_us() + seconds * 1_000_000)

        run_write_transaction(self._write, "defer_notification", write)

    def claim(
        self, session_id: str, mailbox_id: str, default: DeliveryPolicy
    ) -> PeerDelivery | None:
        """Claim at most twenty unread messages; cooldown and hourly budget survive restarts."""

        def write(session: Session) -> PeerDelivery | None:
            now = now_epoch_us()
            row = self._state(session, session_id, mailbox_id)
            if row.retry_at > now:
                return None
            policy = self._policy(session, row, default)
            if not policy.enabled:
                row.retry_at = now + 60_000_000
                return None
            if row.window_start + 3_600_000_000 <= now:
                row.window_start, row.attempts = now, 0
            if row.attempts >= 6:
                row.retry_at = row.window_start + 3_600_000_000
                return None
            messages = list(
                session.scalars(
                    select(SqlPeerMessage)
                    .where(
                        SqlPeerMessage.workspace_id == current_workspace_id(),
                        SqlPeerMessage.recipient_session_id == session_id,
                        SqlPeerMessage.recipient_mailbox_id == mailbox_id,
                        SqlPeerMessage.created_at > row.cursor,
                        SqlPeerMessage.expires_at > now,
                        SqlPeerMessage.acknowledged_at.is_(None),
                    )
                    .order_by(SqlPeerMessage.created_at, SqlPeerMessage.id)
                    .limit(20)
                )
            )
            if not messages:
                return None
            if row.pending_ids is None:
                row.pending_ids = json.dumps([message.id for message in messages])
                row.pending_cursor = messages[-1].created_at
            assert row.pending_ids is not None
            ids = tuple(json.loads(row.pending_ids))
            assert row.pending_cursor is not None
            cursor = row.pending_cursor
            lease = uuid.uuid4().hex
            row.lease = lease
            row.attempts += 1
            row.retry_at = now + 300_000_000
            return PeerDelivery(
                session_id,
                mailbox_id,
                lease,
                hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                ids,
                cursor,
            )

        return run_write_transaction(self._write, "claim_notification", write)

    def finish(self, delivery: PeerDelivery, *, accepted: bool) -> bool:
        """Only the current lease may move the notification cursor; never acknowledge messages."""

        def write(session: Session) -> bool:
            row = self._state(session, delivery.session_id, delivery.mailbox_id)
            if row.lease != delivery.lease:
                return False
            row.lease = None
            row.retry_at = now_epoch_us() + (60_000_000 if accepted else 30_000_000)
            if accepted:
                row.cursor = max(row.cursor, delivery.cursor)
                row.pending_ids = None
                row.pending_cursor = None
            return True

        return run_write_transaction(self._write, "finish_notification", write)
