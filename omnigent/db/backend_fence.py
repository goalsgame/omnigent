"""Fence host-tunnel writes from superseded backend connections."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBackendRoute, current_workspace_id


@dataclass(frozen=True)
class BackendFence:
    workspace_id: int
    host_id: str
    token: str


_fence: ContextVar[BackendFence | None] = ContextVar("backend_fence", default=None)


@contextmanager
def backend_fence_scope(host_id: str, token: str) -> Iterator[None]:
    handle = _fence.set(BackendFence(current_workspace_id(), host_id, token))
    try:
        yield
    finally:
        _fence.reset(handle)


def current_backend_fence() -> BackendFence | None:
    """Return the socket generation bound to the current host handler."""
    return _fence.get()


def host_fence_valid(session: Session, host_id: str, *, require_live: bool = True) -> bool:
    """Lock placement before host writes; this is additional to account authorization."""
    fence = _fence.get()
    if fence is None:
        return True
    if fence.workspace_id != current_workspace_id() or fence.host_id != host_id:
        return False
    row = session.scalar(
        select(SqlBackendRoute)
        .where(
            SqlBackendRoute.workspace_id == fence.workspace_id,
            SqlBackendRoute.kind == "host",
            SqlBackendRoute.resource_id == host_id,
        )
        .with_for_update()
    )
    return (
        row is not None
        and (row.connection_token or row.token) == fence.token
        and (not require_live or row.expires_at > time.time())
    )
