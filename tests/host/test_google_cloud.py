"""Metadata compatibility and credential isolation for managed hosts."""

import threading
from unittest.mock import Mock

import httpx
import pytest

from omnigent.host.google_cloud import GoogleCloudMetadataServer, start_host_google_cloud


@pytest.fixture
def metadata():
    server = GoogleCloudMetadataServer("https://unused.example", "host", "launch-secret")
    server.credential = Mock(
        return_value={
            "token": "short-lived-token",
            "email": "person@example.com",
            "expires_in": 3600,
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
    ) as client:
        yield server, client
    server.shutdown()
    server.server_close()
    thread.join()


def test_metadata_requires_header_and_never_vends_identity_tokens(metadata):
    server, client = metadata
    assert (
        client.get("/computeMetadata/v1/instance/service-accounts/default/token").status_code
        == 403
    )
    for path in (
        "instance/service-accounts/default/identity",
        "instance/attributes/secret",
        "instance/id",
    ):
        response = client.get("/computeMetadata/v1/" + path, headers={"Metadata-Flavor": "Google"})
        assert response.status_code == 404
    server.credential.assert_not_called()


def test_metadata_serves_only_connected_account(metadata):
    _, client = metadata
    headers = {"Metadata-Flavor": "Google"}
    root = "/computeMetadata/v1/instance/service-accounts/"
    response = client.get(root + "default/token", headers=headers)
    assert response.json() == {
        "access_token": "short-lived-token",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(root + "other@example.com/token", headers=headers).status_code == 404
    account = client.get(root + "default/?recursive=true", headers=headers).json()
    assert account["email"] == "person@example.com"
    assert "refresh_token" not in account


def test_disconnect_is_observed_without_metadata_cache(metadata):
    server, client = metadata
    path = "/computeMetadata/v1/instance/service-accounts/default/token"
    headers = {"Metadata-Flavor": "Google"}
    assert client.get(path, headers=headers).status_code == 200
    server.credential.side_effect = ValueError("private diagnostic")
    response = client.get(path, headers=headers)
    assert response.status_code == 403
    assert "Connect Google Cloud" in response.text
    assert "private diagnostic" not in response.text


def test_disabled_adapter_does_not_override_personal_credentials(monkeypatch):
    monkeypatch.delenv("OMNIGENT_GOOGLE_CLOUD_AUTH", raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/personal/config")
    assert start_host_google_cloud("https://unused.example", "host") is None
    import os

    assert os.environ["CLOUDSDK_CONFIG"] == "/personal/config"
