"""Fresh installs and upgrades preserve existing grants without inventing membership."""

from __future__ import annotations

from importlib import import_module

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlSessionPermission
from omnigent.db.group_authority import group_principal
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


def test_upgrade_keeps_existing_keys_and_separates_case_colliding_groups(db_uri):
    migration = import_module(
        "omnigent.db.migrations.versions.og1a2b3c4d5e_add_device_grant_group_authority"
    )
    table = SqlSessionPermission.__table__
    first, second = group_principal("aaa"), group_principal("aaG")
    conversation_id = "1" * 32
    engine = sa.create_engine(db_uri)
    with engine.begin() as connection:
        # Recreate the legacy MySQL comparison before running the upgrade.
        if connection.dialect.name == "mysql":
            connection.execute(
                sa.text(
                    "ALTER TABLE session_permissions MODIFY user_id "
                    "VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL"
                )
            )
        connection.execute(
            table.insert(),
            [
                {
                    "workspace_id": 0,
                    "user_id": first,
                    "conversation_id": conversation_id,
                    "level": 1,
                },
                {
                    "workspace_id": 0,
                    "user_id": "Person@example.test",
                    "conversation_id": conversation_id,
                    "level": 2,
                },
            ],
        )
        if connection.dialect.name == "mysql":
            assert (
                connection.scalar(sa.select(table.c.level).where(table.c.user_id == second)) == 1
            )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.scalar(sa.select(table.c.level).where(table.c.user_id == second))
                is None
            )
            connection.execute(
                table.insert().values(
                    workspace_id=0,
                    user_id=second,
                    conversation_id=conversation_id,
                    level=3,
                )
            )
            expected = {first: 1, second: 3, "Person@example.test": 2}
            assert (
                dict(connection.execute(sa.select(table.c.user_id, table.c.level)).all())
                == expected
            )
            # Downgrade must not collapse the two group keys back together.
            migration.downgrade()
            assert (
                dict(connection.execute(sa.select(table.c.user_id, table.c.level)).all())
                == expected
            )
            migration.upgrade()
            assert (
                dict(connection.execute(sa.select(table.c.user_id, table.c.level)).all())
                == expected
            )
    engine.dispose()
