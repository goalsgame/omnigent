"""Fresh and upgraded databases can index the host-scoped consent lookup."""

import uuid

from alembic import command
from sqlalchemy import insert, inspect, select

from omnigent.db.db_models import SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

INDEX = "ix_conversation_metadata_host_kind"
TABLE = "omnigent_conversation_metadata"


def test_host_consent_index_on_fresh_database(db_uri):
    db_engine = get_or_create_engine(db_uri)
    indexes = {index["name"]: index for index in inspect(db_engine).get_indexes(TABLE)}
    assert indexes[INDEX]["column_names"] == ["workspace_id", "host_id", "kind"]
    assert not indexes[INDEX]["unique"]


def test_host_consent_index_upgrade_and_downgrade(db_uri):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    session_id = uuid.uuid4().hex
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "nn1a2b3c4d5e")
        assert INDEX not in {index["name"] for index in inspect(connection).get_indexes(TABLE)}
        connection.execute(
            insert(SqlConversationMetadata).values(
                id=session_id, kind=1, google_cloud_access="saved-consent"
            )
        )
        command.upgrade(config, "head")
        indexes = {index["name"]: index for index in inspect(connection).get_indexes(TABLE)}
        assert indexes[INDEX]["column_names"] == ["workspace_id", "host_id", "kind"]
        assert (
            connection.scalar(
                select(SqlConversationMetadata.google_cloud_access).where(
                    SqlConversationMetadata.id == session_id
                )
            )
            == "saved-consent"
        )
