"""Docker startup must expose configured Google Cloud connections to the UI."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deploy.docker.entrypoint import _ResolvedConfig, build_app
from tests.server.test_github_store import SecretBox


@pytest.fixture
def docker_config(monkeypatch, tmp_path: Path, db_uri: str):
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_AUTH", "1")
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_CLIENT_ID", "test-client")
    monkeypatch.setenv("OMNIGENT_GOOGLE_CLOUD_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "OMNIGENT_GOOGLE_CLOUD_REDIRECT_URI",
        "https://app.example/v1/connections/google_cloud/callback",
    )
    monkeypatch.setattr(
        "omnigent.stores.credential_store.build_secret_cipher", lambda: SecretBox("test-key")
    )
    return _ResolvedConfig(
        cfg={},
        database_url=db_uri,
        artifact_dir=tmp_path,
        artifact_store_uri=None,
        host="127.0.0.1",
        port=8000,
    )


def test_docker_exposes_google_cloud_connection(docker_config):
    app = build_app(docker_config).app
    client = TestClient(app, headers={"X-Forwarded-Email": "person@example.com"})
    info = client.get("/v1/info")
    assert info.status_code == 200
    assert "google_cloud" in info.json()["enabled_connections"]
    status = client.get("/v1/connections/google_cloud/status")
    assert status.status_code == 200
    assert status.json()["connected"] is False
    assert app.state.google_cloud_store is not None


def test_docker_google_cloud_opt_in_required(docker_config, monkeypatch):
    monkeypatch.delenv("OMNIGENT_GOOGLE_CLOUD_AUTH")
    app = build_app(docker_config).app
    client = TestClient(app, headers={"X-Forwarded-Email": "person@example.com"})
    assert "google_cloud" not in client.get("/v1/info").json()["enabled_connections"]
    assert app.state.google_cloud_store is None


def test_docker_google_cloud_requires_encryption(docker_config, monkeypatch):
    monkeypatch.setattr("omnigent.stores.credential_store.build_secret_cipher", lambda: None)
    with pytest.raises(
        RuntimeError, match="Google Cloud connections require credential encryption"
    ):
        build_app(docker_config)
