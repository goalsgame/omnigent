"""Store session digests and their searchable entries.

Revision ID: oh1b2c3d4e5f
Revises: of1b2c3d4e5f

The pgvector embedding column is added at startup by
``ensure_session_memory_vectors`` when session memory is configured, since its
dimension comes from the configured embedding model.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "oh1b2c3d4e5f"
down_revision = "of1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create digest and entry tables."""
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "session_digests" not in tables:
        op.create_table(
            "session_digests",
            sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
            sa.Column("conversation_id", Uuid16(), primary_key=True),
            sa.Column("digest", sa.Text(), nullable=True),
            sa.Column("source_updated_at", sa.Integer(), nullable=False),
            sa.Column("items_through", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("error", sa.String(512), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.Integer(), nullable=False),
            sa.Column("lease_until", sa.Integer(), nullable=False),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("created_at", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.Integer(), nullable=True),
        )
    if "session_memory_entries" not in tables:
        op.create_table(
            "session_memory_entries",
            sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
            sa.Column("id", Uuid16(), primary_key=True),
            sa.Column("conversation_id", Uuid16(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("item_ids", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_session_memory_entries_conversation",
            "session_memory_entries",
            ["workspace_id", "conversation_id", "ordinal"],
        )


def downgrade() -> None:
    """Remove all session memory."""
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "session_memory_entries" in tables:
        op.drop_table("session_memory_entries")
    if "session_digests" in tables:
        op.drop_table("session_digests")
