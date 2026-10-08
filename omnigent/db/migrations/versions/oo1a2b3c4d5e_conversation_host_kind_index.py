"""Index host-scoped session consent lookups.

Revision ID: oo1a2b3c4d5e
Revises: nn1a2b3c4d5e
"""

from alembic import op

revision = "oo1a2b3c4d5e"
down_revision = "nn1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_metadata_host_kind",
        "omnigent_conversation_metadata",
        ["workspace_id", "host_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_host_kind", table_name="omnigent_conversation_metadata"
    )
