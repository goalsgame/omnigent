"""Fresh and upgrade migration paths preserve existing session metadata."""

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


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
