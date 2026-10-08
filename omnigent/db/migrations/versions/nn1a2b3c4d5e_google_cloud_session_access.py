"""Add default-denied session Google Cloud access.

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
"""

import sqlalchemy as sa
from alembic import op

revision = "nn1a2b3c4d5e"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("google_cloud_access", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("omnigent_conversation_metadata", "google_cloud_access")
