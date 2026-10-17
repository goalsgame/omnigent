"""Peer catalog migration and cross-dialect schema contract."""

import importlib
import uuid

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlPeerMessage
from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_peer_migration_and_orm_schema_agree(db_uri):
    engine = get_or_create_engine(db_uri)
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.ob1b2c3d4e5f_add_peer_messages"
    )
    table = SqlPeerMessage.__table__
    with engine.begin() as connection:
        table.drop(connection)
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                columns = {c["name"]: c for c in sa.inspect(connection).get_columns(table.name)}
                assert columns["body"]["default"] is None
                assert not columns["body"]["nullable"]
                assert columns["acknowledged_at"]["nullable"]
                assert len(sa.inspect(connection).get_indexes(table.name)) == 3
                migration.downgrade()
            table.create(connection)
        finally:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()


def test_upgrade_from_artifacts_preserves_data(tmp_path):
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "oa1b2c3d4e5f")
    engine = sa.create_engine(uri)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE upgrade_marker (value TEXT)"))
        conn.execute(sa.text("INSERT INTO upgrade_marker VALUES ('preserved')"))
        metadata = sa.Table("omnigent_conversation_metadata", sa.MetaData(), autoload_with=conn)
        for _ in range(2):
            conn.execute(metadata.insert().values(workspace_id=0, id=uuid.uuid4().bytes, kind=1))

    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert "peer_messages" in sa.inspect(conn).get_table_names()
        assert conn.scalar(sa.text("SELECT value FROM upgrade_marker")) == "preserved"
        incarnations = (
            conn.execute(sa.text("SELECT peer_mailbox_id FROM omnigent_conversation_metadata"))
            .scalars()
            .all()
        )
        assert incarnations == [None, None]
    from omnigent.db.db_models import SqlConversationMetadata
    from omnigent.stores.peer_message_store import PeerMessageStore

    store = PeerMessageStore(uri)
    with get_or_create_engine(uri).connect() as conn:
        session_ids = conn.execute(sa.select(SqlConversationMetadata.id)).scalars().all()
    incarnations = [store.initialize_mailbox_id(sid, "local") for sid in session_ids]
    assert len(incarnations) == len(set(incarnations)) == 2
    assert all(store.mailbox_id(sid) for sid in session_ids)

    command.downgrade(config, "oa1b2c3d4e5f")
    assert "peer_messages" not in sa.inspect(engine).get_table_names()
    engine.dispose()
