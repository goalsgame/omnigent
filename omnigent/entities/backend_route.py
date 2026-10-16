"""Expiring placement of a backend-owned resource."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendRoute:
    workspace_id: int
    kind: str
    resource_id: str
    replica_id: str
    endpoint: str
    token: str
    expires_at: float
    metadata: dict[str, object] | None = None
    connection_token: str | None = None
