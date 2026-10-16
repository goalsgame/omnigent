"""Backend placement schema upgrades preserve existing session metadata."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.backend_route_store import BackendRouteStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_PREVIOUS = "og1a2b3c4d5e"
_REVISION = "oh1a2b3c4d5e"


def migrate(engine, uri, revision, *, downgrade=False):
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            from omnigent.db.cockroachdb import (
                _crdb_server_version,
                _prepare_crdb_schema_transaction,
            )

            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)
        connection.commit()


def assert_schema(engine):
    with engine.connect() as connection:
        # A regular statement refreshes SQLite's pooled schema cache after DDL.
        connection.execute(sa.text("SELECT * FROM omnigent_backend_routes LIMIT 0"))
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint("omnigent_backend_routes")["constrained_columns"] == [
            "workspace_id",
            "kind",
            "resource_id",
        ]
        assert any(
            index["column_names"] == ["expires_at"]
            for index in inspector.get_indexes("omnigent_backend_routes")
        )


def test_upgrade_and_downgrade_preserve_existing_sessions(db_uri):
    engine = get_or_create_engine(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    original = store.create_conversation(title="keep this session")
    try:
        migrate(engine, db_uri, _PREVIOUS, downgrade=True)
        assert "omnigent_backend_routes" not in sa.inspect(engine).get_table_names()
        assert store.get_conversation(original.id).title == "keep this session"
        migrate(engine, db_uri, _REVISION)
        assert_schema(engine)
        if engine.dialect.name in {"sqlite", "postgresql"}:
            routes = BackendRouteStore(db_uri, "a", "http://127.0.0.1:8001")
            assert routes.claim("session", original.id).replica_id == "a"
        migrate(engine, db_uri, _PREVIOUS, downgrade=True)
        assert "omnigent_backend_routes" not in sa.inspect(engine).get_table_names()
        assert store.get_conversation(original.id).title == "keep this session"
    finally:
        migrate(engine, db_uri, "head")


def test_fresh_schema_supports_resource_placement(tmp_path: Path):
    uri = f"sqlite:///{tmp_path / 'fresh.db'}"
    engine = sa.create_engine(uri)
    try:
        migrate(engine, uri, "head")
        assert_schema(engine)
        assert (
            BackendRouteStore(uri, "a", "http://127.0.0.1:8001")
            .claim("host", "example")
            .replica_id
            == "a"
        )
    finally:
        engine.dispose()
