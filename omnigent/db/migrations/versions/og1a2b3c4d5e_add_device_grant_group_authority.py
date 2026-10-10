"""Persist bounded OIDC group authority on refresh grants.

Revision ID: og1a2b3c4d5e
Revises: oo1a2b3c4d5e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "og1a2b3c4d5e"
down_revision = "oo1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "group_authority_json" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("device_grants")
    }:
        op.add_column("device_grants", sa.Column("group_authority_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_grants") as batch:
        batch.drop_column("group_authority_json")
