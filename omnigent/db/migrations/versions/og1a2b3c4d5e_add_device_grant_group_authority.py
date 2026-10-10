"""Persist bounded OIDC group authority and compare permission keys exactly.

MySQL permission keys use binary collation, including on downgrade, so distinct
group principals cannot merge under a case-insensitive database default.

Revision ID: og1a2b3c4d5e
Revises: oo1a2b3c4d5e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "og1a2b3c4d5e"
down_revision = "oo1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        column = next(
            column
            for column in sa.inspect(op.get_bind()).get_columns("session_permissions")
            if column["name"] == "user_id"
        )
        if getattr(column["type"], "collation", None) != "utf8mb4_bin":
            op.alter_column(
                "session_permissions",
                "user_id",
                existing_type=column["type"],
                existing_nullable=False,
                type_=VARCHAR(128, charset="utf8mb4", collation="utf8mb4_bin"),
            )
    if "group_authority_json" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("device_grants")
    }:
        op.add_column("device_grants", sa.Column("group_authority_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_grants") as batch:
        batch.drop_column("group_authority_json")
