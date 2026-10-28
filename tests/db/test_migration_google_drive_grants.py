"""Google Drive grant migration and ORM schema agree."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlGoogleDriveGrant
from omnigent.db.utils import get_or_create_engine


def test_google_drive_grant_migration_and_orm_schema_agree(db_uri):
    engine = get_or_create_engine(db_uri)
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.oe1b2c3d4e5f_add_google_drive_grants"
    )
    table = SqlGoogleDriveGrant.__table__
    with engine.begin() as connection:
        table.drop(connection)
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                columns = {c["name"]: c for c in sa.inspect(connection).get_columns(table.name)}
                assert set(columns) == {c.name for c in table.columns}
                assert columns["file_id"]["nullable"]
                assert not columns["audience"]["nullable"]
                assert [i["name"] for i in sa.inspect(connection).get_indexes(table.name)] == [
                    "ix_google_drive_grants_session"
                ]
                migration.downgrade()
                assert table.name not in sa.inspect(connection).get_table_names()
            table.create(connection)
        finally:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
