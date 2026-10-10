"""Fresh installs and upgrades preserve existing grants without inventing membership."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_group_authority_upgrade_and_downgrade_preserve_grants(tmp_path):
    uri = f"sqlite:///{tmp_path / 'groups.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "oo1a2b3c4d5e")
        connection.execute(
            sa.text(
                "INSERT INTO device_grants (workspace_id, id, device_code_hash, user_code, "
                "status, user_id, created_at, expires_at) "
                "VALUES (0, 'existing', 'hash', 'CODE', 1, 'member', 1, 2)"
            )
        )
        command.upgrade(config, "head")
        assert (
            connection.execute(
                sa.text("SELECT group_authority_json FROM device_grants WHERE id='existing'")
            ).scalar_one()
            is None
        )
        command.downgrade(config, "oo1a2b3c4d5e")
        assert (
            connection.execute(
                sa.text("SELECT user_id FROM device_grants WHERE id='existing'")
            ).scalar_one()
            == "member"
        )
    engine.dispose()
