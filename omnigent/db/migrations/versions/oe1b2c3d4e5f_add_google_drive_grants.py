"""Add per-session Google Drive grants.

Revision ID: oe1b2c3d4e5f
Revises: od1b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "oe1b2c3d4e5f"
down_revision = "od1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create grant storage without session foreign keys."""
    if "google_drive_grants" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "google_drive_grants",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column("connection_generation", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("file_id", sa.String(256), nullable=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("mime_type", sa.String(256), nullable=True),
        sa.Column("access", sa.String(8), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_google_drive_grants_session", "google_drive_grants", ["workspace_id", "session_id"]
    )
    op.create_index(
        "ux_google_drive_grants_scope",
        "google_drive_grants",
        ["workspace_id", "session_id", "user_id", "scope_key"],
        unique=True,
    )


def downgrade() -> None:
    """Remove all Drive grants."""
    if "google_drive_grants" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("google_drive_grants")
