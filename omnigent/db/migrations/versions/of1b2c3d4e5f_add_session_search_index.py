"""Index message text for ranked session search.

Revision ID: of1b2c3d4e5f
Revises: oe1b2c3d4e5f

PostgreSQL only. The GIN index covers messages and compaction summaries and is
built concurrently so writers are not blocked.
"""

from __future__ import annotations

from alembic import op

from omnigent.db.utils import drop_session_search_index, ensure_session_search_index

revision = "of1b2c3d4e5f"
down_revision = "oe1b2c3d4e5f"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Build the full-text index on PostgreSQL; other dialects search without it."""
    if not _is_postgres():
        return
    with op.get_context().autocommit_block():
        ensure_session_search_index(op.get_bind())


def downgrade() -> None:
    """Drop the full-text index."""
    if not _is_postgres():
        return
    with op.get_context().autocommit_block():
        drop_session_search_index(op.get_bind())
