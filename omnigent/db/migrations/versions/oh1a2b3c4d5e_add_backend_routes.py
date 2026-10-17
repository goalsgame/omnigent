"""Add fenced backend resource ownership.

Revision ID: oh1a2b3c4d5e
Revises: og1a2b3c4d5e
"""

import sqlalchemy as sa
from alembic import op

revision = "oh1a2b3c4d5e"
down_revision = "og1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "omnigent_backend_routes",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("resource_id", sa.String(128), primary_key=True),
        sa.Column("replica_id", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("connection_token", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("reported_metadata", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_omnigent_backend_routes_expires_at", "omnigent_backend_routes", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_omnigent_backend_routes_expires_at", "omnigent_backend_routes")
    op.drop_table("omnigent_backend_routes")
