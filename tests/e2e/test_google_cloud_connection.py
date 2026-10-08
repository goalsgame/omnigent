"""Offline OAuth-to-host credential flow with fake Google responses only."""

import socket
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import Session

from omnigent.connections.google_cloud import GoogleCloudConnectionStore
from omnigent.db.db_models import SqlConversationMetadata, SqlHost, SqlSessionPermission
from omnigent.db.utils import get_or_create_engine
from omnigent.host.google_cloud import GoogleCloudMetadataServer
from omnigent.server.auth import AuthProvider
from omnigent.server.google_cloud import SCOPES, GoogleCloudConfig
from omnigent.server.routes.connections_google_cloud import create_connections_google_cloud_router
from omnigent.server.routes.host_credentials import create_host_credentials_router
from tests.server.test_github_store import SecretBox


class UserAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("X-Test-User")


def test_oauth_to_sandbox_token_refresh_and_disconnect(db_uri):
    user = "person@example.com"
    store = GoogleCloudConnectionStore(db_uri, SecretBox("fixture-key"))
    host_id, session_id = uuid.uuid4().hex, uuid.uuid4().hex
    with Session(get_or_create_engine(db_uri)) as db:
        db.add(
            SqlHost(
                host_id=host_id,
                user_id=user,
                name="sandbox",
                status=1,
                created_at=1,
                updated_at=1,
                sandbox_provider="agent_sandbox",
            )
        )
        db.add(
            SqlConversationMetadata(id=session_id, host_id=host_id, workspace="/workspace", kind=1)
        )
        db.add(SqlSessionPermission(user_id=user, conversation_id=session_id, level=4))
        db.commit()
    config = GoogleCloudConfig("fixture", "fixture-secret", "https://app.example/callback")
    api = SimpleNamespace(
        token=AsyncMock(
            return_value={
                "access_token": "access-one",
                "refresh_token": "server-only-refresh",
                "expires_at": time.time() + 3600,
                "scope": SCOPES,
            }
        ),
        identity=AsyncMock(return_value={"subject": "google-user", "email": user}),
    )
    hosts = SimpleNamespace(
        resolve_launch_token=lambda host, token: (
            SimpleNamespace(user_id=user) if (host, token) == (host_id, "launch") else None
        )
    )
    app = FastAPI()
    app.state.google_cloud_store, app.state.google_cloud_client = store, api
    app.include_router(
        create_connections_google_cloud_router(
            config, store, auth_provider=UserAuth(), client=api
        ),
        prefix="/v1",
    )
    app.include_router(create_host_credentials_router(hosts), prefix="/v1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            address = f"http://127.0.0.1:{sock.getsockname()[1]}"
            metadata = GoogleCloudMetadataServer(address, host_id, "launch")
            try:
                with httpx.Client(
                    base_url=address, headers={"X-Test-User": user}, trust_env=False
                ) as client:
                    root = "/v1/connections/google_cloud"
                    start = client.get(root + "/connect", params={"return_to": "/settings"})
                    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                    callback = {"state": state, "code": "one-use-google-code"}
                    wrong = client.get(
                        root + "/callback",
                        params=callback,
                        headers={"X-Test-User": "other@example.com"},
                    )
                    assert "google_cloud=error" in wrong.headers["location"]
                    api.token.assert_not_awaited()
                    result = client.get(root + "/callback", params=callback)
                    assert result.headers["location"] == "/settings?google_cloud=connected"
                    assert client.get(root + "/status").json()["email"] == user
                    import pytest

                    from omnigent.host.google_cloud import GoogleCloudAccessRequired

                    with pytest.raises(GoogleCloudAccessRequired):
                        metadata.credential()
                    consent_url = f"/v1/connections/google_cloud/sessions/{session_id}/access"
                    consent = client.get(consent_url).json()
                    assert consent["state"] == "pending"
                    decision = client.post(
                        consent_url,
                        json={"decision": "allowed", "generation": consent["generation"]},
                    )
                    assert decision.status_code == 200
                    assert metadata.credential()["token"] == "access-one"
                    connection = store.get(user, with_tokens=True)
                    store.refresh(
                        user,
                        connection=connection,
                        tokens={"access_token": "expired", "expires_at": 0},
                    )
                    api.token.return_value = {
                        "access_token": "access-two",
                        "expires_at": time.time() + 3600,
                    }
                    assert metadata.credential()["token"] == "access-two"
                    api.token.assert_awaited_with(
                        {"grant_type": "refresh_token", "refresh_token": "server-only-refresh"}
                    )
                    assert client.post(root + "/disconnect").json()["disconnected"]
                    import pytest

                    with pytest.raises(ValueError, match="unavailable"):
                        metadata.credential()
            finally:
                metadata.server_close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
