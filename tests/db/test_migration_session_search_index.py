"""Session search full-text index migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import CONVERSATION_ITEMS_SEARCH_INDEX
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

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
