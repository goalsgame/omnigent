"""Persist peer notification policies, cursors and leases.

Revision ID: od1b2c3d4e5f
Revises: oc1b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "od1b2c3d4e5f"
down_revision = "oc1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add notification state without rewriting existing mailboxes or messages."""
    if "peer_deliveries" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "peer_deliveries",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("mailbox_id", Uuid16(), primary_key=True),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pending_ids", sa.Text(), nullable=True),
        sa.Column("pending_cursor", sa.BigInteger(), nullable=True),
        sa.Column("lease", sa.String(32), nullable=True),
        sa.Column("retry_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_peer_deliveries_session_id", "peer_deliveries", ["session_id"])


def downgrade() -> None:
    """Remove delivery state while preserving messages and acknowledgements."""
    op.drop_table("peer_deliveries")
