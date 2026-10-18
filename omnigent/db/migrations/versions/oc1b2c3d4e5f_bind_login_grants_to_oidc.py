"""Bind local login refresh grants to the originating offline OIDC generation."""

import sqlalchemy as sa
from alembic import op

revision = "oc1b2c3d4e5f"
down_revision = "ob1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "oidc_login_generation" in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("device_grants")
    }:
        return
    op.add_column(
        "device_grants", sa.Column("oidc_login_generation", sa.String(48), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("device_grants", "oidc_login_generation")
