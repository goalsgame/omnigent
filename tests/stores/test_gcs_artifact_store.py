"""GCS configuration, key isolation, and error translation."""

from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.cloud import storage

from omnigent.stores.artifact_store.gcs import GCSArtifactStore, _parse_gcs_uri


@pytest.fixture
def client():
    return MagicMock(spec=storage.Client)


@pytest.mark.parametrize("prefix", ["", "/artifacts"])
def test_bucket_and_prefix(client, prefix):
    store = GCSArtifactStore(f"gs://test-bucket{prefix}", client=client)
    blob = client.bucket.return_value.blob.return_value
    blob.download_as_bytes.return_value = b"\x00\xffpayload"
    blob.exists.return_value = True
    key = "agents/a/bundle.tar.gz"
    store.put(key, b"\x00\xffpayload")
    assert store.get(key) == b"\x00\xffpayload"
    assert store.exists(key)
    store.delete(key)
    client.bucket.assert_called_once_with("test-bucket")
    assert all(
        call.args == (f"{prefix.lstrip('/') + '/' if prefix else ''}{key}",)
        for call in client.bucket.return_value.blob.call_args_list
    )
    blob.upload_from_string.assert_called_once_with(
        b"\x00\xffpayload", content_type="application/octet-stream", timeout=60
    )
    blob.download_as_bytes.assert_called_once_with(timeout=60, raw_download=True)
    blob.delete.assert_called_once_with(timeout=60)


def test_missing_and_permission_errors(client):
    store = GCSArtifactStore("gs://test-bucket", client=client)
    blob = client.bucket.return_value.blob.return_value
    blob.download_as_bytes.side_effect = NotFound("missing")
    with pytest.raises(KeyError, match="missing-key"):
        store.get("missing-key")
    blob.delete.side_effect = NotFound("missing")
    store.delete("missing-key")
    for name, method in [
        ("download_as_bytes", store.get),
        ("delete", store.delete),
        ("exists", store.exists),
    ]:
        getattr(blob, name).side_effect = Forbidden("denied")
        with pytest.raises(Forbidden):
            method("key")
    blob.upload_from_string.side_effect = Forbidden("denied")
    with pytest.raises(Forbidden):
        store.put("key", b"data")


@pytest.mark.parametrize(
    "key", ["", ".", "..", "../key", "a/../key", "/key", "a\\b", "C:/key", "x\x00"]
)
def test_rejects_unsafe_keys_before_io(client, key):
    store = GCSArtifactStore("gs://test-bucket/artifacts", client=client)
    for method, args in [
        (store.put, (key, b"x")),
        (store.get, (key,)),
        (store.exists, (key,)),
        (store.delete, (key,)),
    ]:
        with pytest.raises(ValueError, match="invalid artifact key"):
            method(*args)
    client.bucket.return_value.blob.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "s3://bucket",
        "gs://",
        "gs://user@bucket/key",
        "gs://bucket:80/key",
        "gs://bucket/key?x=y",
        "gs://bucket/key#x",
        "gs://bucket/../key",
        "gs://bucket/a\\b",
        "gs://bucket%2fother/key",
    ],
)
def test_rejects_invalid_uris(uri):
    with pytest.raises(ValueError):
        _parse_gcs_uri(uri)


def test_default_client_uses_adc(monkeypatch):
    constructor = MagicMock(spec=storage.Client)
    monkeypatch.setattr(storage, "Client", constructor)
    GCSArtifactStore("gs://test-bucket/artifacts")
    constructor.assert_called_once_with()


def test_both_server_entrypoints_select_gcs(monkeypatch, tmp_path):
    from deploy.docker.entrypoint import _ResolvedConfig, _select_artifact_store
    from omnigent.cli import _create_artifact_store

    monkeypatch.setattr(storage, "Client", MagicMock(spec=storage.Client))
    uri = "gs://test-bucket/artifacts"
    resolved = _ResolvedConfig(
        cfg={},
        database_url="sqlite://",
        artifact_dir=tmp_path,
        artifact_store_uri=uri,
        host="127.0.0.1",
        port=8000,
    )
    assert isinstance(_select_artifact_store(resolved), GCSArtifactStore)
    assert isinstance(_create_artifact_store(str(tmp_path), store_uri=uri), GCSArtifactStore)


def test_cli_rejects_unknown_remote_scheme(tmp_path):
    from omnigent.cli import _create_artifact_store

    with pytest.raises(ValueError, match="artifact_store_uri"):
        _create_artifact_store(str(tmp_path), store_uri="https://untrusted.example")
