"""Encrypted per-user Google Cloud grants."""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from omnigent.connections import ConnectionStore
from omnigent.connections.google_cloud.access import GoogleCloudSessionAccess
from omnigent.entities import ProviderConnection
from omnigent.stores.credential_store.secret_cipher import SecretCipher


class GoogleCloudConnectionStore(ConnectionStore[ProviderConnection]):
    _PROVIDER: ClassVar[str] = "google_cloud"

    def __init__(self, storage_location: str, secret_cipher: SecretCipher) -> None:
        super().__init__(storage_location, secret_cipher)
        self.access = GoogleCloudSessionAccess(storage_location)

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> ProviderConnection:
        return conn

    def upsert(self, user_id: str, *, tokens: dict[str, Any], identity: dict[str, str]) -> None:
        self._store.upsert(
            user_id,
            self._PROVIDER,
            secret={
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
            },
            metadata={
                "generation": secrets.token_hex(16),
                **identity,
                "expires_at": tokens["expires_at"],
                "scopes": tokens["scope"],
            },
        )

    def refresh(
        self, user_id: str, *, tokens: dict[str, Any], connection: ProviderConnection
    ) -> bool:
        return self._store.update_secret(
            user_id,
            self._PROVIDER,
            secret={
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token")
                or (connection.secret or {})["refresh_token"],
            },
            metadata={**connection.metadata, "expires_at": tokens["expires_at"]},
            expected_metadata=connection.metadata,
        )
