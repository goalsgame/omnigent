"""Session search full-text index migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import CONVERSATION_ITEMS_SEARCH_INDEX
from omnigent.db.utils import (
    _build_alembic_config,
    _ensure_conversation_tables,
    clear_engine_cache,
    drop_session_search_index,
    ensure_session_search_index,
    get_or_create_engine,
)

_REVISION = "of1b2c3d4e5f"
_PREVIOUS = "oe1b2c3d4e5f"


def _indexes(engine: sa.Engine) -> set[str]:
    return {index["name"] for index in sa.inspect(engine).get_indexes("conversation_items")}


def _migrate(uri: str, engine: sa.Engine, target: str) -> None:
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        if target == _REVISION:
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
        connection.commit()


def test_migration_is_a_reversible_no_op_on_sqlite(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'search.db'}"
    engine = sa.create_engine(uri)
    try:
        for target in (_REVISION, _PREVIOUS, _REVISION):
            _migrate(uri, engine, target)
            assert CONVERSATION_ITEMS_SEARCH_INDEX not in _indexes(engine)
    finally:
        engine.dispose()
        clear_engine_cache()


def test_migration_builds_a_valid_index_on_postgres(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL full-text index")
    try:
        _migrate(db_uri, engine, _PREVIOUS)
        assert CONVERSATION_ITEMS_SEARCH_INDEX not in _indexes(engine)
    finally:
        _migrate(db_uri, engine, _REVISION)
    with engine.connect() as connection:
        valid = connection.execute(
            sa.text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"
            ),
            {"name": CONVERSATION_ITEMS_SEARCH_INDEX},
        ).scalar_one()
    assert valid


def test_conversation_bootstrap_adds_the_index_to_an_existing_postgres_table(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL full-text index")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {CONVERSATION_ITEMS_SEARCH_INDEX}")
        )
    assert CONVERSATION_ITEMS_SEARCH_INDEX not in _indexes(engine)

    _ensure_conversation_tables(engine)

    assert CONVERSATION_ITEMS_SEARCH_INDEX in _indexes(engine)


def test_index_is_built_per_partition_on_a_partitioned_table(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL full-text index")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA IF EXISTS search_partitions CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA search_partitions"))
        try:
            connection.execute(sa.text("SET search_path TO search_partitions"))
            for statement in (
                "CREATE TABLE conversation_items (type smallint, search_text text, "
                "created_at integer) PARTITION BY RANGE (created_at)",
                "CREATE TABLE items_old PARTITION OF conversation_items "
                "FOR VALUES FROM (0) TO (1000)",
                "CREATE TABLE items_new PARTITION OF conversation_items "
                "FOR VALUES FROM (1000) TO (2000) PARTITION BY RANGE (created_at)",
                "CREATE TABLE items_new_a PARTITION OF items_new FOR VALUES FROM (1000) TO (1500)",
                "CREATE TABLE items_new_b PARTITION OF items_new FOR VALUES FROM (1500) TO (2000)",
            ):
                connection.execute(sa.text(statement))

            ensure_session_search_index(connection)
            connection.execute(
                sa.text(
                    "CREATE TABLE items_later PARTITION OF conversation_items "
                    "FOR VALUES FROM (2000) TO (3000)"
                )
            )
            ensure_session_search_index(connection)

            valid = connection.execute(
                sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
                {"name": CONVERSATION_ITEMS_SEARCH_INDEX},
            ).scalar_one()
            leaves = connection.execute(
                sa.text(
                    "SELECT count(*) FROM pg_indexes WHERE schemaname = 'search_partitions' "
                    "AND tablename IN ('items_old', 'items_new_a', 'items_new_b', 'items_later') "
                    "AND indexdef LIKE '%to_tsvector%'"
                )
            ).scalar_one()
            assert valid
            assert leaves == 4

            drop_session_search_index(connection)
            assert (
                connection.execute(
                    sa.text("SELECT to_regclass(:name)"), {"name": CONVERSATION_ITEMS_SEARCH_INDEX}
                ).scalar()
                is None
            )
        finally:
            connection.execute(sa.text("SET search_path TO public"))
            connection.execute(sa.text("DROP SCHEMA search_partitions CASCADE"))
