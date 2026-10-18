"""Preserve existing local refresh grants while adding nullable OIDC bindings."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_upgrade_preserves_existing_grants_and_tolerates_current_metadata(tmp_path):
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.oc1b2c3d4e5f_bind_login_grants_to_oidc"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE device_grants (id TEXT PRIMARY KEY, "
                    "refresh_token_hash TEXT NOT NULL)"
                )
            )
            connection.execute(
                sa.text("INSERT INTO device_grants VALUES ('existing', 'preserved-hash')")
            )
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
                migration.upgrade()
            row = connection.execute(
                sa.text("SELECT id, refresh_token_hash, oidc_login_generation FROM device_grants")
            ).one()
            assert tuple(row) == ("existing", "preserved-hash", None)
    finally:
        engine.dispose()
