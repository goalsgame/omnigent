"""Commit a new session's initial owner alongside its metadata."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBackendRoute, current_workspace_id
from omnigent.entities.backend_route import BackendRoute


@dataclass
class CreationPlacement:
    replica_id: str
    endpoint: str
    ttl: float
    active: bool = True
    created: list[BackendRoute] = field(default_factory=list)


_placement: ContextVar[CreationPlacement | None] = ContextVar(
    "session_creation_placement", default=None
)


@contextmanager
def creation_placement_scope(
    replica_id: str,
    endpoint: str,
    ttl: float,
) -> Iterator[list[BackendRoute]]:
    placement = CreationPlacement(replica_id, endpoint, ttl)
    token = _placement.set(placement)
    try:
        yield placement.created
    finally:
        placement.active = False
        _placement.reset(token)


def insert_initial_session_placement(session: Session, session_id: str) -> None:
    """Prevent another backend taking a session before its launch task starts."""
    placement = _placement.get()
    if placement is None or not placement.active:
        return
    route = BackendRoute(
        current_workspace_id(),
        "session",
        session_id,
        placement.replica_id,
        placement.endpoint,
        uuid.uuid4().hex,
        time.time() + placement.ttl,
    )
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    session.execute(
        insert(SqlBackendRoute)
        .values(
            workspace_id=route.workspace_id,
            kind=route.kind,
            resource_id=route.resource_id,
            replica_id=route.replica_id,
            endpoint=route.endpoint,
            token=route.token,
            expires_at=route.expires_at,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "kind", "resource_id"])
    )
    placement.created.append(route)
