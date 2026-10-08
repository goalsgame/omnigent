"""Metadata compatibility and credential isolation for managed hosts."""

import threading
from unittest.mock import Mock

import httpx
import pytest

from omnigent.host.google_cloud import (
    GoogleCloudMetadataServer,
    GoogleCloudNotConnected,
    start_host_google_cloud,
)


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
    server.credential.side_effect = GoogleCloudNotConnected("private diagnostic")
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


@pytest.mark.parametrize("failure", ["disconnected", "timeout", "unavailable", "malformed"])
def test_metadata_distinguishes_missing_connection_from_broker_failure(
    metadata, monkeypatch, failure
):
    server, client = metadata
    server.credential = GoogleCloudMetadataServer.credential.__get__(server)

    def broker_get(url, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("private diagnostic")
        return httpx.Response(
            503 if failure == "unavailable" else 200,
            request=httpx.Request("GET", url),
            json={"connected": False}
            if failure == "disconnected"
            else {"error": "private diagnostic"},
        )

    monkeypatch.setattr(httpx, "get", broker_get)
    response = client.get(
        "/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    assert response.status_code == (403 if failure == "disconnected" else 503)
    assert ("Connect Google Cloud" in response.text) == (failure == "disconnected")
    assert "private diagnostic" not in response.text


def test_enabled_adapter_clears_inherited_named_gcloud_config(monkeypatch, tmp_path):
    import os

    from omnigent.host.identity_env import HOST_TOKEN_ENV_VAR

    for key, value in {
        "HOME": str(tmp_path),
        "IS_SANDBOX": "1",
        "OMNIGENT_GOOGLE_CLOUD_AUTH": "1",
        HOST_TOKEN_ENV_VAR: "fixture-launch",
        "CLOUDSDK_ACTIVE_CONFIG_NAME": "personal",
        "CLOUDSDK_CONFIG": "/personal/config",
        "GCE_METADATA_HOST": "old",
        "GCE_METADATA_ROOT": "old",
        "GCE_METADATA_IP": "old",
    }.items():
        monkeypatch.setenv(key, value)
    server = start_host_google_cloud("https://unused.example", "host")
    assert server is not None
    try:
        assert "CLOUDSDK_ACTIVE_CONFIG_NAME" not in os.environ
        assert os.environ["CLOUDSDK_CONFIG"] == str(tmp_path / ".omnigent/gcloud")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("state", ["pending", "off", "denied", "unavailable", "not_connected"])
def test_metadata_consent_guidance_matches_available_action(metadata, monkeypatch, state):
    server, client = metadata
    server.credential = GoogleCloudMetadataServer.credential.__get__(server)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"connected": False, "reason": f"session_access_{state}"},
        ),
    )
    response = client.get(
        "/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert ("approve, then retry" in response.text) == (state in ("pending", "off", "denied"))
    assert ("dedicated managed sandbox" in response.text) == (state == "unavailable")
    assert ("Connect Google Cloud" in response.text) == (state == "not_connected")
