"""Add durable, independently shared published artifacts.

Revision ID: oa1b2c3d4e5f
Revises: og1a2b3c4d5e
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

from omnigent.db.db_models import Uuid16

revision = "oa1b2c3d4e5f"
down_revision = "og1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the publication catalog without session foreign keys."""
    if "published_artifacts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "published_artifacts",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(128).with_variant(
                VARCHAR(128, charset="utf8mb4", collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("owner_generation", sa.String(32), nullable=True),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("grants_json", sa.Text(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_published_artifacts_expires_at", "published_artifacts", ["expires_at"])


def downgrade() -> None:
    """Remove publication metadata; stored blobs require operator cleanup."""
    if "published_artifacts" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("published_artifacts")
