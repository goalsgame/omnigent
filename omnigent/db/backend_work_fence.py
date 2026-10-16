"""Fence background launch work at database and provider boundaries."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBackendRoute
from omnigent.entities.backend_route import BackendRoute

_T = TypeVar("_T")


@dataclass
class BackendWorkFence:
    database_uri: str
    routes: dict[tuple[int, str, str], BackendRoute] = field(default_factory=dict)
    lost: bool = False
    parent: BackendWorkFence | None = None

    def check(self, session: Session) -> None:
        routes = dict(self.routes)
        ancestor: BackendWorkFence | None = self
        while ancestor is not None:
            if ancestor.lost:
                self.lost = True
                raise asyncio.CancelledError("backend ownership lost")
            for key, route in ancestor.routes.items():
                if key in routes and routes[key].token != route.token:
                    self.lost = True
                    raise asyncio.CancelledError("backend ownership changed within a request")
                routes[key] = route
            ancestor = ancestor.parent
        for key, route in sorted(routes.items()):
            row = session.scalar(
                select(SqlBackendRoute)
                .where(
                    SqlBackendRoute.workspace_id == key[0],
                    SqlBackendRoute.kind == key[1],
                    SqlBackendRoute.resource_id == key[2],
                )
                .with_for_update()
            )
            if row is None or row.token != route.token or row.expires_at <= time.time():
                self.lost = True
                raise asyncio.CancelledError("backend ownership lost")


_current: ContextVar[BackendWorkFence | None] = ContextVar("backend_launch_fence", default=None)


@contextmanager
def work_fence_scope(fence: BackendWorkFence | None) -> Iterator[None]:
    token = _current.set(fence)
    try:
        yield
    finally:
        _current.reset(token)


def current_work_fence() -> BackendWorkFence | None:
    """Return the ownership scope inherited by nested mutations."""
    return _current.get()


def check_backend_work(session: Session | None = None) -> None:
    fence = _current.get()
    if fence is None:
        return
    from omnigent.db.utils import get_or_create_engine

    engine = get_or_create_engine(fence.database_uri)
    if session is not None:
        if session.get_bind() is not engine:
            raise ValueError("replication cannot fence writes to a separate database")
        fence.check(session)
    else:
        with Session(engine) as authority:
            fence.check(authority)


@contextmanager
def backend_work_transaction(session: Session) -> Iterator[None]:
    """Lock placement before lifecycle writes and validate again before commit."""
    check_backend_work(session)
    yield
    check_backend_work(session)


async def fenced_to_thread(function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    def call() -> _T:
        check_backend_work()
        result = function(*args, **kwargs)
        check_backend_work()
        return result

    return await asyncio.to_thread(call)


async def guarded_work(coroutine: Coroutine[Any, Any, _T], fence: BackendWorkFence) -> _T:
    with work_fence_scope(fence):
        try:
            await asyncio.to_thread(check_backend_work)
            return await coroutine
        finally:
            coroutine.close()
