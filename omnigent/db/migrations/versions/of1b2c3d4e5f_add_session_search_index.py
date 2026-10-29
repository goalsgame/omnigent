"""Index message text for ranked session search.

Revision ID: of1b2c3d4e5f
Revises: oe1b2c3d4e5f

PostgreSQL only. The GIN index covers messages and compaction summaries and is
built concurrently so writers are not blocked.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import CONVERSATION_ITEMS_SEARCH_INDEX

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
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": CONVERSATION_ITEMS_SEARCH_INDEX},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {CONVERSATION_ITEMS_SEARCH_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {CONVERSATION_ITEMS_SEARCH_INDEX} "
            "ON conversation_items USING gin "
            "(to_tsvector('english', left(search_text, 100000))) "
            "WHERE type IN (1, 6)"
        )


def downgrade() -> None:
    """Drop the full-text index."""
    if not _is_postgres():
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {CONVERSATION_ITEMS_SEARCH_INDEX}")
