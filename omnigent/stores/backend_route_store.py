"""Shared resource placement and fencing for active backend replicas."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBackendRoute, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.entities.backend_route import BackendRoute


def _metadata(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _route(row: SqlBackendRoute) -> BackendRoute:
    return BackendRoute(
        row.workspace_id,
        row.kind,
        row.resource_id,
        row.replica_id,
        row.endpoint,
        row.token,
        row.expires_at,
        _metadata(row.reported_metadata),
        row.connection_token or row.token,
    )


class BackendRouteStore:
    """Atomic placement claims backed by shared PostgreSQL (SQLite for local use).

    Replicas need unique process-lifetime IDs. An existing live remote owner
    keeps its resource; a reconnect to the same replica replaces the fencing
    token. Expired ownership can be reclaimed by another replica.
    """

    def __init__(
        self,
        database_uri: str,
        replica_id: str,
        endpoint: str,
        *,
        ttl: float = 90,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not replica_id or len(replica_id) > 64:
            raise ValueError("replica_id must contain 1–64 characters")
        if not endpoint or len(endpoint) > 512:
            raise ValueError("replica endpoint must contain 1–512 characters")
        if not 15 <= ttl <= 300:
            raise ValueError("backend route TTL must be between 15 and 300 seconds")
        engine = get_or_create_engine(database_uri)
        if engine.dialect.name not in ("postgresql", "sqlite"):
            raise ValueError("backend replication requires PostgreSQL or SQLite")
        self._insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
        self._session = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.backend_routes",
            immediate=True,
        )
        self.replica_id = replica_id
        self.endpoint = endpoint
        self.ttl = ttl
        self._now = now

    @staticmethod
    def _key(kind: str, resource_id: str):
        if not kind or len(kind) > 32 or not resource_id or len(resource_id) > 128:
            raise ValueError("invalid backend routing resource")
        return and_(
            SqlBackendRoute.workspace_id == current_workspace_id(),
            SqlBackendRoute.kind == kind,
            SqlBackendRoute.resource_id == resource_id,
        )

    def get(self, kind: str, resource_id: str) -> BackendRoute | None:
        """Return fresh placement in this workspace, without disclosing other tenants."""
        with self._session("resolve_resource_owner") as session:
            row = session.scalar(
                select(SqlBackendRoute).where(
                    self._key(kind, resource_id),
                    SqlBackendRoute.expires_at > self._now(),
                )
            )
            return _route(row) if row is not None else None

    def is_standalone_runner(self, runner_id: str) -> bool:
        """A runner-owned lease records origin even after the socket disconnects."""
        with self._session("resolve_runner_origin") as session:
            return (
                session.scalar(
                    select(SqlBackendRoute.resource_id).where(self._key("runner", runner_id))
                )
                is not None
            )

    def peer_routes(self) -> list[BackendRoute]:
        """One fresh routing proof per other replica in the current workspace."""
        with self._session("discover_backend_peers") as session:
            rows = session.scalars(
                select(SqlBackendRoute).where(
                    SqlBackendRoute.workspace_id == current_workspace_id(),
                    SqlBackendRoute.expires_at > self._now(),
                    SqlBackendRoute.replica_id != self.replica_id,
                )
            )
            peers: dict[str, BackendRoute] = {}
            for row in rows:
                peers.setdefault(row.endpoint, _route(row))
            return list(peers.values())

    def host_metadata(self, host_ids: list[str]) -> dict[str, dict[str, object]]:
        if not host_ids:
            return {}
        with self._session("read_connected_host_metadata") as session:
            rows = session.scalars(
                select(SqlBackendRoute).where(
                    SqlBackendRoute.workspace_id == current_workspace_id(),
                    SqlBackendRoute.kind == "host",
                    SqlBackendRoute.resource_id.in_(host_ids),
                    SqlBackendRoute.expires_at > self._now(),
                    SqlBackendRoute.reported_metadata.is_not(None),
                )
            )
            return {
                row.resource_id: value
                for row in rows
                if (value := _metadata(row.reported_metadata)) is not None
            }

    def claim(self, kind: str, resource_id: str) -> BackendRoute:
        """Claim a vacant/expired resource, or return its live remote owner."""
        return self._claim(kind, resource_id, None)[0]

    def claim_connection(
        self, kind: str, resource_id: str, previous: BackendRoute | None
    ) -> tuple[BackendRoute, bool]:
        """Rotate a connection and prove continuity under the same row lock."""
        return self._claim(kind, resource_id, previous)

    def _claim(
        self, kind: str, resource_id: str, previous: BackendRoute | None
    ) -> tuple[BackendRoute, bool]:
        key = self._key(kind, resource_id)

        def write(session: Session) -> tuple[BackendRoute, bool]:
            prior = session.scalar(select(SqlBackendRoute).where(key).with_for_update())
            now = self._now()
            continuous = (
                previous is not None
                and prior is not None
                and previous.workspace_id == current_workspace_id()
                and previous.kind == kind
                and previous.resource_id == resource_id
                and prior.token == previous.token
                and prior.replica_id == self.replica_id
                and prior.endpoint == self.endpoint
                and prior.expires_at > now
            )
            lease_token = prior.token if continuous and prior is not None else uuid.uuid4().hex
            values = {
                "workspace_id": current_workspace_id(),
                "kind": kind,
                "resource_id": resource_id,
                "replica_id": self.replica_id,
                "endpoint": self.endpoint,
                "token": lease_token,
                "connection_token": uuid.uuid4().hex if continuous else lease_token,
                "expires_at": now + self.ttl,
                "reported_metadata": None,
            }
            statement = self._insert(SqlBackendRoute).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["workspace_id", "kind", "resource_id"],
                set_={
                    name: values[name]
                    for name in (
                        "replica_id",
                        "endpoint",
                        "token",
                        "connection_token",
                        "expires_at",
                        "reported_metadata",
                    )
                },
                where=or_(
                    SqlBackendRoute.expires_at <= now,
                    SqlBackendRoute.replica_id == self.replica_id,
                    SqlBackendRoute.endpoint == self.endpoint,
                ),
            )
            session.execute(statement)
            row = session.scalar(
                select(SqlBackendRoute).where(key).execution_options(populate_existing=True)
            )
            assert row is not None
            return _route(row), continuous

        return run_write_transaction(self._session, "claim_resource_owner", write)

    def renew(self, route: BackendRoute, reported: dict[str, object] | None = None) -> bool:
        """Extend only a still-live, matching generation; never resurrect a lost lease."""

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlBackendRoute)
                .where(
                    self._key(route.kind, route.resource_id),
                    SqlBackendRoute.replica_id == self.replica_id,
                    SqlBackendRoute.token == route.token,
                )
                .with_for_update()
            )
            # Evaluate time after waiting for the row lock, not before it.
            now = self._now()
            if row is None or row.expires_at <= now:
                return False
            row.expires_at = now + self.ttl
            if reported is not None and (row.connection_token or row.token) == (
                route.connection_token or route.token
            ):
                row.reported_metadata = json.dumps(reported)
            return True

        return run_write_transaction(self._session, "renew_resource_owner", write)

    def release(self, route: BackendRoute) -> bool:
        """Release routing for this generation, retaining standalone runner identity."""

        def write(session: Session) -> bool:
            if route.kind == "runner":
                # Keep the authenticated standalone origin separate from liveness.
                statement = update(SqlBackendRoute).values(expires_at=0, reported_metadata=None)
            else:
                statement = delete(SqlBackendRoute)
            result = session.execute(
                statement.where(
                    self._key(route.kind, route.resource_id),
                    SqlBackendRoute.replica_id == self.replica_id,
                    SqlBackendRoute.token == route.token,
                    SqlBackendRoute.expires_at != 0,
                ).returning(SqlBackendRoute.token)
            )
            return result.scalar_one_or_none() is not None

        return run_write_transaction(self._session, "release_resource_owner", write)
