"""Add durable session peer messages.

Revision ID: ob1b2c3d4e5f
Revises: oa1b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "ob1b2c3d4e5f"
down_revision = "oa1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create inbox storage without runner-dependent state or session foreign keys."""
    bind = op.get_bind()
    metadata = sa.table(
        "omnigent_conversation_metadata",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("peer_mailbox_id", Uuid16()),
    )
    if "peer_mailbox_id" not in {
        column["name"] for column in sa.inspect(bind).get_columns(metadata.name)
    }:
        op.add_column(metadata.name, sa.Column("peer_mailbox_id", Uuid16(), nullable=True))
    if "peer_messages" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "peer_messages",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("sender_session_id", Uuid16(), nullable=False),
        sa.Column("recipient_session_id", Uuid16(), nullable=False),
        sa.Column("sender_user_id", sa.String(128), nullable=False),
        sa.Column("sender_generation", sa.String(32), nullable=True),
        sa.Column("sender_mailbox_id", Uuid16(), nullable=False),
        sa.Column("recipient_mailbox_id", Uuid16(), nullable=False),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reply_to", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_peer_messages_inbox",
        "peer_messages",
        ["workspace_id", "recipient_session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_peer_messages_outbox",
        "peer_messages",
        ["workspace_id", "sender_session_id", "created_at", "id"],
    )
    op.create_index("ix_peer_messages_expires_at", "peer_messages", ["expires_at"])


def downgrade() -> None:
    """Remove the messageboard catalog."""
    if "peer_messages" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("peer_messages")
    if "peer_mailbox_id" in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }:
        with op.batch_alter_table("omnigent_conversation_metadata") as batch:
            batch.drop_column("peer_mailbox_id")
