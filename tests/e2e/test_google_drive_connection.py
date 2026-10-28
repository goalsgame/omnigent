"""Offline OAuth, session grant and runner tool flow with fake Google responses only."""

import asyncio
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.connections.google_drive import GoogleDriveConnectionStore
from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import group_authority_scope
from omnigent.errors import OmnigentError
from omnigent.runner.google_drive import execute_google_drive_tool
from omnigent.server.auth import AuthProvider
from omnigent.server.google_drive import DOCUMENT, SCOPES, GoogleDriveClient, GoogleDriveConfig
from omnigent.server.routes.connections_google_drive import create_connections_google_drive_router
from omnigent.server.routes.google_drive import create_google_drive_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.google_drive_grant_store import GoogleDriveGrantStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_github_store import SecretBox

USER = "person@example.com"
DOC = "doc1234567890"
OTHER = "other123456789"
ACCESS_TOKEN = "google-access-secret"
REFRESH_TOKEN = "google-refresh-secret"


class UserAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("X-Test-User")

    def get_credential_user_id(self, request):
        return request.headers.get("X-Test-User")


class FakeGoogle(GoogleDriveClient):
    async def token(self, fields):
        assert fields["code"] == "one-use-google-code"
        return {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 3600,
            "scope": SCOPES,
        }

    async def identity(self, access_token):
        assert access_token == ACCESS_TOKEN
        return {"subject": "google-user", "email": USER}


def _google(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    files = {DOC: "Plan", OTHER: "Salaries"}
    for file_id, name in files.items():
        base = f"/drive/v3/files/{file_id}"
        if request.url.path == base:
            return httpx.Response(
                200,
                json={
                    "id": file_id,
                    "name": name,
                    "mimeType": DOCUMENT,
                    "capabilities": {"canEdit": True},
                },
            )
        if request.url.path == f"{base}/export":
            return httpx.Response(200, content=f"# {name}\n\nShip it".encode())
    return httpx.Response(404, json={"error": {"message": "not found"}})


def test_oauth_grant_and_agent_read_without_token_exposure(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    store = GoogleDriveConnectionStore(db_uri, SecretBox("fixture-key"))
    session_id = conversations.create_conversation().id
    permissions.ensure_user(USER)
    permissions.grant(USER, session_id, 4)
    config = GoogleDriveConfig("fixture", "fixture-secret", "https://app.example/callback")
    api = FakeGoogle(config, httpx.MockTransport(_google))
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with account_authority_scope(None, None), group_authority_scope(), workspace_scope(0):
            return await call_next(request)

    app.include_router(
        create_connections_google_drive_router(
            config, store, auth_provider=UserAuth(), client=api
        ),
        prefix="/v1",
    )
    app.include_router(
        create_google_drive_router(
            store, api, GoogleDriveGrantStore(db_uri), conversations, permissions, UserAuth()
        ),
        prefix="/v1",
    )
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
            with httpx.Client(base_url=address, headers={"X-Test-User": USER}) as client:
                root = "/v1/connections/google_drive"
                start = client.get(root + "/connect", params={"return_to": "/settings"})
                state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                callback = client.get(
                    root + "/callback", params={"state": state, "code": "one-use-google-code"}
                )
                assert callback.headers["location"] == "/settings?google_drive=connected"
                status = client.get(root + "/status").json()
                assert status["email"] == USER

                grants = f"/v1/sessions/{session_id}/google-drive"
                added = client.post(
                    grants + "/grants",
                    json={
                        "kind": "file",
                        "file": f"https://docs.google.com/document/d/{DOC}/edit",
                        "access": "read",
                        "generation": status["generation"],
                    },
                )
                assert added.status_code == 200, added.text
                [grant] = added.json()["grants"]

                async def agent(tool, args):
                    async with httpx.AsyncClient(
                        base_url=address, headers={"X-Test-User": USER}
                    ) as runner:
                        return await execute_google_drive_tool(tool, args, session_id, runner)

                read = asyncio.run(agent("google_drive_read", {"file_id": DOC}))
                assert json.loads(read)["content"] == "# Plan\n\nShip it"
                listed = asyncio.run(agent("google_drive_list", {}))
                for output in (read, listed, json.dumps(client.get(grants).json())):
                    assert ACCESS_TOKEN not in output and REFRESH_TOKEN not in output

                other = json.loads(asyncio.run(agent("google_drive_read", {"file_id": OTHER})))
                assert "not available to this session" in other["error"]

                assert client.delete(f"{grants}/grants/{grant['id']}").status_code == 200
                revoked = json.loads(asyncio.run(agent("google_drive_read", {"file_id": DOC})))
                assert "error" in revoked
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
