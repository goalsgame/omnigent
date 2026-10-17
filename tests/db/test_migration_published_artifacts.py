"""Publication catalog DDL works through both migrations and ORM creation."""

import importlib

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlPublishedArtifact
from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_publication_migration_and_metadata_accept_text_without_server_default(db_uri):
    engine = get_or_create_engine(db_uri)
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.oa1b2c3d4e5f_add_published_artifacts"
    )
    table = SqlPublishedArtifact.__table__
    with engine.begin() as connection:
        table.drop(connection)
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                columns = {
                    column["name"]: column
                    for column in sa.inspect(connection).get_columns(table.name)
                }
                assert columns["grants_json"]["default"] is None
                assert not columns["grants_json"]["nullable"]
                assert "owner_generation" in columns
                migration.downgrade()
            table.create(connection)
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns(table.name)
            }
            assert columns["grants_json"]["default"] is None
        finally:
            table.create(connection, checkfirst=True)


def test_upgrade_adds_independent_catalog_and_preserves_session_data(tmp_path):
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "og1a2b3c4d5e")
    engine = sa.create_engine(uri)
    with engine.begin() as connection:
        assert "published_artifacts" not in sa.inspect(connection).get_table_names()
        # Use a separate marker table to check non-artifact schema/data survives.
        connection.execute(sa.text("CREATE TABLE migration_marker (value TEXT)"))
        connection.execute(sa.text("INSERT INTO migration_marker VALUES ('preserved')"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert "published_artifacts" in inspector.get_table_names()
        assert not inspector.get_foreign_keys("published_artifacts")
        assert connection.scalar(sa.text("SELECT value FROM migration_marker")) == "preserved"
        columns = {column["name"] for column in inspector.get_columns("published_artifacts")}
        assert {"owner_id", "source_session_id", "ready", "grants_json", "expires_at"} <= columns
    command.downgrade(config, "og1a2b3c4d5e")
    assert "published_artifacts" not in sa.inspect(engine).get_table_names()
    engine.dispose()
