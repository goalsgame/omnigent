"""Shared Google Cloud Storage artifacts using Application Default Credentials."""

from __future__ import annotations

from contextlib import suppress
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from omnigent.stores.artifact_store import ArtifactStore

if TYPE_CHECKING:
    from google.cloud import storage


def _validate_key(key: str) -> None:
    if (
        not PurePosixPath(key).parts
        or ".." in PurePosixPath(key).parts
        or "\\" in key
        or "\x00" in key
        or PurePosixPath(key).is_absolute()
        or PureWindowsPath(key).is_absolute()
    ):
        raise ValueError(f"invalid artifact key: {key!r}")


def _parse_gcs_uri(location: str) -> tuple[str, str]:
    uri = urlsplit(location)
    if (
        uri.scheme != "gs"
        or not uri.netloc
        or uri.netloc != uri.hostname
        or ":" in uri.netloc
        or "%" in uri.netloc
        or "\\" in uri.netloc
        or uri.query
        or uri.fragment
    ):
        raise ValueError("artifact storage must be a 'gs://bucket[/prefix]' URI")
    prefix = uri.path.strip("/")
    if prefix:
        _validate_key(prefix)
    return uri.netloc, prefix


class GCSArtifactStore(ArtifactStore):
    """Store artifacts under ``gs://bucket[/prefix]``, shared across servers.

    Authentication uses the Google SDK's Application Default Credentials,
    including GKE Workload Identity. The bucket must already exist and the
    identity needs object read, write and delete permissions in that bucket.

    :param storage_location: Bucket URI with an optional object prefix.
    :param client: Optional SDK client, for an emulator or custom credentials.
    """

    def __init__(self, storage_location: str, client: storage.Client | None = None) -> None:
        bucket, self._prefix = _parse_gcs_uri(storage_location)
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise ImportError("GCS artifacts require 'pip install omnigent[gcs]'.") from exc

        super().__init__(storage_location)
        self._client = client if client is not None else storage.Client()
        self._bucket = self._client.bucket(bucket)

    def _object_name(self, key: str) -> str:
        _validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    def put(self, key: str, data: bytes) -> None:
        """Atomically replace an object with the supplied bytes."""
        self._bucket.blob(self._object_name(key)).upload_from_string(
            data, content_type="application/octet-stream", timeout=60
        )

    def get(self, key: str) -> bytes:
        """Read an object, raising KeyError when it is missing."""
        from google.api_core.exceptions import NotFound

        blob = self._bucket.blob(self._object_name(key))
        try:
            return blob.download_as_bytes(timeout=60, raw_download=True)
        except NotFound:
            raise KeyError(key) from None

    def delete(self, key: str) -> None:
        """Delete an object; a missing object is a no-op."""
        from google.api_core.exceptions import NotFound

        blob = self._bucket.blob(self._object_name(key))
        with suppress(NotFound):
            blob.delete(timeout=60)

    def exists(self, key: str) -> bool:
        """Check an object's metadata without downloading its content."""
        return self._bucket.blob(self._object_name(key)).exists(timeout=60)
