"""Per-session Google Drive grants, sharing acknowledgement and agent operations."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.connections.google_drive import GoogleDriveConnectionStore
from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlGoogleDriveGrant, workspace_scope
from omnigent.db.group_authority import (
    bind_group_authority,
    group_authority_scope,
    group_principal,
)
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.google_drive import (
    DOCUMENT,
    FOLDER,
    SCOPES,
    SPREADSHEET,
    GoogleDriveConfig,
    GoogleDriveError,
)
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.server.routes.connections_google_drive import (
    GoogleDriveConnectionHooks,
    create_connections_google_drive_router,
)
from omnigent.server.routes.google_drive import create_google_drive_router, parse_file_id
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.google_drive_grant_store import GoogleDriveGrantStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_github_store import SecretBox

OWNER = "owner@example.test"
BOB = "bob@example.test"
READER = "reader@example.test"
DOC = "doc_aaaaaaaaaaaa"
SHEET = "sheet_bbbbbbbbbb"
PRIVATE = "private_cccccccc"
FOLDER_ID = "folder_dddddddd"


class _Auth(AuthProvider):
    def get_user_id(self, request):
        user = request.headers.get("x-user")
        if user:
            bind_group_authority(
                user, json.loads(request.headers.get("x-groups", "[]")), int(time.time()) + 60
            )
        return user

    def get_credential_user_id(self, request):
        if request.headers.get("x-delegated"):
            return None
        return request.headers.get("x-user")


class FakeDrive:
    """Drive API double keyed by access token, so each account sees only its files."""

    def __init__(self) -> None:
        self.files = {
            "token-owner": {
                DOC: {"name": "Plan", "mime_type": DOCUMENT, "can_edit": True},
                SHEET: {"name": "Budget", "mime_type": SPREADSHEET, "can_edit": False},
                PRIVATE: {"name": "Salaries", "mime_type": DOCUMENT, "can_edit": True},
                FOLDER_ID: {"name": "Team", "mime_type": FOLDER, "can_edit": True},
            },
            "token-bob": {SHEET: {"name": "Budget", "mime_type": SPREADSHEET, "can_edit": True}},
        }
        self.calls: list[tuple] = []
        self.on_create = None

    def _meta(self, token, file_id):
        try:
            item = self.files[token][file_id]
        except KeyError:
            raise GoogleDriveError("File not found or not accessible", 404) from None
        return {"id": file_id, "web_view_link": f"https://drive/{file_id}", **item}

    async def metadata(self, token, file_id):
        return self._meta(token, file_id)

    async def search(self, token, query, limit=20):
        self.calls.append(("search", token, query))
        return [
            self._meta(token, file_id)
            for file_id, item in self.files[token].items()
            if item["mime_type"] != FOLDER and query.lower() in item["name"].lower()
        ][:limit]

    async def read(self, token, file, sheet_range):
        self.calls.append(("read", token, file["id"], sheet_range))
        return {"format": "text/markdown", "content": f"# {file['name']}", "truncated": False}

    async def create(self, token, title, kind, content):
        self.calls.append(("create", token, title, kind, content))
        if self.on_create:
            self.on_create()
        file_id = "created_eeeeeeee"
        self.files[token][file_id] = {"name": title, "mime_type": DOCUMENT, "can_edit": True}
        return self._meta(token, file_id)

    async def append(self, token, file_id, text):
        self.calls.append(("append", token, file_id, text))

    async def update_values(self, token, file_id, sheet_range, values):
        self.calls.append(("values", token, file_id, sheet_range, values))
        return {"updated_range": sheet_range, "updated_cells": sum(map(len, values))}


@pytest.fixture
def env(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    connections = GoogleDriveConnectionStore(db_uri, SecretBox("test-key"))
    grants = GoogleDriveGrantStore(db_uri)
    drive = FakeDrive()
    session = conversations.create_conversation().id
    other = conversations.create_conversation().id
    child = conversations.create_conversation(parent_conversation_id=session).id
    for user in (OWNER, BOB, READER):
        permissions.ensure_user(user)
    permissions.grant(OWNER, session, 4)
    permissions.grant(OWNER, other, 4)

    def connect(user, token):
        connections.upsert(
            user,
            tokens={
                "access_token": token,
                "refresh_token": "refresh",
                "expires_at": time.time() + 3600,
                "scope": SCOPES,
            },
            identity={"subject": user, "email": user},
        )

    connect(OWNER, "token-owner")
    connect(BOB, "token-bob")
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with account_authority_scope(None, None), group_authority_scope(), workspace_scope(0):
            return await call_next(request)

    config = GoogleDriveConfig("client", "secret", "https://app.example/callback")
    app.include_router(
        create_connections_google_drive_router(
            config, connections, auth_provider=_Auth(), client=drive
        ),
        prefix="/v1",
    )
    app.include_router(
        create_google_drive_router(
            connections, drive, grants, conversations, permissions, _Auth()
        ),
        prefix="/v1",
    )
    return SimpleNamespace(
        client=TestClient(app),
        drive=drive,
        permissions=permissions,
        connections=connections,
        grants=grants,
        connect=connect,
        session=session,
        other=other,
        child=child,
    )


def _generation(env, user=OWNER):
    return env.connections.get(user).metadata["generation"]


def grant(env, user=OWNER, session=None, audience=None, **body):
    payload = {"generation": _generation(env, user), "access": "read", **body}
    if audience is not None:
        payload["audience"] = audience
    return env.client.post(
        f"/v1/sessions/{session or env.session}/google-drive/grants",
        json=payload,
        headers={"x-user": user},
    )


def agent(env, method, path="", session=None, user=OWNER, headers=None, **kwargs):
    return getattr(env.client, method)(
        f"/v1/sessions/{session or env.session}/google-drive/agent/files{path}",
        headers={"x-user": user, **(headers or {})},
        **kwargs,
    )


def snapshot(env, user=OWNER, session=None):
    return env.client.get(
        f"/v1/sessions/{session or env.session}/google-drive", headers={"x-user": user}
    ).json()


def test_parse_file_id_accepts_ids_and_links():
    assert parse_file_id("1AbC_def-ghij") == "1AbC_def-ghij"
    assert (
        parse_file_id("https://docs.google.com/document/d/1AbC_def-ghij/edit?tab=t.0")
        == "1AbC_def-ghij"
    )
    assert parse_file_id("https://drive.google.com/open?id=1AbC_def-ghij") == "1AbC_def-ghij"
    assert parse_file_id("not a link") is None


def test_file_grant_allows_only_that_file(env):
    response = grant(env, kind="file", file=f"https://docs.google.com/document/d/{DOC}/edit")
    assert response.status_code == 200, response.text
    [item] = response.json()["grants"]
    assert item | {"id": None} == item | {
        "id": None,
        "kind": "file",
        "file_id": DOC,
        "name": "Plan",
        "access": "read",
        "account": OWNER,
        "mine": True,
        "state": "active",
    }

    read = agent(env, "get", f"/{DOC}")
    assert read.status_code == 200, read.text
    assert read.json()["content"] == "# Plan"
    assert read.json()["file"]["account"] == OWNER

    denied = agent(env, "get", f"/{PRIVATE}")
    assert denied.status_code == 403
    assert "Google Drive control" in denied.json()["detail"]
    listed = agent(env, "get").json()
    assert [f["id"] for f in listed["granted_files"]] == [DOC]
    assert listed["search_results"] == []
    assert not any(call[0] == "search" for call in env.drive.calls)


def test_read_grant_cannot_edit_and_write_grant_can(env):
    grant(env, kind="file", file=DOC)
    assert agent(env, "post", f"/{DOC}/append", json={"text": "x"}).status_code == 403
    assert grant(env, kind="file", file=DOC, access="write").status_code == 200
    assert len(snapshot(env)["grants"]) == 1
    response = agent(env, "post", f"/{DOC}/append", json={"text": "Next steps"})
    assert response.status_code == 200, response.text
    assert ("append", "token-owner", DOC, "Next steps") in env.drive.calls


def test_file_grant_validation(env):
    assert grant(env, kind="file", file=FOLDER_ID).status_code == 422
    assert grant(env, kind="file", file=SHEET, access="write").status_code == 422
    assert grant(env, kind="file", file="missing_file_id").status_code == 404
    assert grant(env, kind="file", file="nope").status_code == 422
    assert grant(env, kind="drive", file=DOC).status_code == 422
    stale = env.client.post(
        f"/v1/sessions/{env.session}/google-drive/grants",
        json={"kind": "drive", "access": "read", "generation": "stale"},
        headers={"x-user": OWNER},
    )
    assert stale.status_code == 409


def test_whole_drive_read_searches_and_reads_but_cannot_create(env):
    assert grant(env, kind="drive").status_code == 200
    listed = agent(env, "get", params={"query": "sal"}).json()
    assert [f["id"] for f in listed["search_results"]] == [PRIVATE]
    assert agent(env, "get", f"/{PRIVATE}").status_code == 200
    create = agent(env, "post", json={"title": "Notes", "kind": "document", "content": "# Hi"})
    assert create.status_code == 403
    assert agent(env, "post", f"/{DOC}/append", json={"text": "x"}).status_code == 403


def test_whole_drive_write_creates_and_grants_new_file(env):
    grant(env, kind="drive", access="write")
    response = agent(env, "post", json={"title": "Notes", "kind": "document", "content": "# Hi"})
    assert response.status_code == 200, response.text
    assert response.json()["file"]["id"] == "created_eeeeeeee"
    assert ("create", "token-owner", "Notes", "document", "# Hi") in env.drive.calls
    kinds = sorted((g["kind"], g["file_id"]) for g in snapshot(env)["grants"])
    assert kinds == [("drive", None), ("file", "created_eeeeeeee")]


def test_shared_session_requires_acknowledging_participants(env):
    env.permissions.grant(BOB, env.session, 2)
    env.permissions.grant(READER, env.session, 1)
    response = grant(env, kind="drive")
    assert response.status_code == 409
    audience = response.json()["detail"]["audience"]
    assert [(a["principal"], a["level"], a["can_use_agent"]) for a in audience] == [
        (BOB, 2, True),
        (READER, 1, False),
    ]
    assert grant(env, kind="drive", audience={BOB: 2}).status_code == 409
    accepted = grant(env, kind="drive", audience={BOB: 2, READER: 1})
    assert accepted.status_code == 200
    assert accepted.json()["grants"][0]["state"] == "active"


def test_sharing_after_grant_pauses_until_owner_reconfirms(env):
    grant(env, kind="drive")
    assert agent(env, "get", f"/{DOC}").status_code == 200
    env.permissions.grant(BOB, env.session, 2)

    [item] = snapshot(env)["grants"]
    assert item["state"] == "suspended"
    assert [a["principal"] for a in item["new_audience"]] == [BOB]
    paused = agent(env, "get", f"/{DOC}")
    assert paused.status_code == 403
    assert "re-confirm" in paused.json()["detail"]

    confirm = f"/v1/sessions/{env.session}/google-drive/grants/{item['id']}/confirm"
    assert (
        env.client.post(confirm, json={"audience": {}}, headers={"x-user": OWNER}).status_code
        == 409
    )
    assert (
        env.client.post(confirm, json={"audience": {BOB: 2}}, headers={"x-user": BOB}).status_code
        == 404
    )
    ok = env.client.post(confirm, json={"audience": {BOB: 2}}, headers={"x-user": OWNER})
    assert ok.status_code == 200
    assert agent(env, "get", f"/{DOC}").status_code == 200


def test_raising_a_reader_to_editor_pauses_but_narrowing_does_not(env):
    env.permissions.grant(READER, env.session, 1)
    grant(env, kind="drive", audience={READER: 1})
    env.permissions.revoke(READER, env.session)
    assert snapshot(env)["grants"][0]["state"] == "active"
    env.permissions.grant(READER, env.session, 2)
    assert snapshot(env)["grants"][0]["state"] == "suspended"


def test_collaborators_and_readers(env):
    env.permissions.grant(BOB, env.session, 2)
    env.permissions.grant(READER, env.session, 1)
    response = grant(
        env, user=BOB, kind="file", file=SHEET, access="write", audience={OWNER: 4, READER: 1}
    )
    assert response.status_code == 200, response.text

    edit = agent(env, "put", f"/{SHEET}/values", json={"range": "A1", "values": [["1", 2]]})
    assert edit.status_code == 200, edit.text
    assert ("values", "token-bob", SHEET, "A1", [["1", 2]]) in env.drive.calls

    reader_view = snapshot(env, READER)
    assert "audience" not in reader_view
    assert reader_view["grants"][0]["account"] == BOB
    assert reader_view["viewer"]["can_grant"] is False
    env.connect(READER, "token-reader")
    assert grant(env, user=READER, kind="drive").status_code == 403
    assert agent(env, "get", user=READER).status_code == 403


def test_revocation_rights(env):
    env.permissions.grant(BOB, env.session, 2)
    grant(env, user=BOB, kind="drive", audience={OWNER: 4})
    [item] = snapshot(env)["grants"]
    path = f"/v1/sessions/{env.session}/google-drive/grants/{item['id']}"
    env.permissions.grant(READER, env.session, 2)
    assert env.client.delete(path, headers={"x-user": READER}).status_code == 403
    assert env.client.delete(path, headers={"x-user": OWNER}).status_code == 200
    assert snapshot(env)["grants"] == []


def test_grantor_can_revoke_own_grant(env):
    env.permissions.grant(BOB, env.session, 2)
    grant(env, user=BOB, kind="drive", audience={OWNER: 4})
    [item] = snapshot(env)["grants"]
    path = f"/v1/sessions/{env.session}/google-drive/grants/{item['id']}"
    assert env.client.delete(path, headers={"x-user": BOB}).status_code == 200


def test_reconnecting_or_losing_session_access_deactivates_grants(env):
    env.permissions.grant(BOB, env.session, 2)
    grant(env, user=BOB, kind="file", file=SHEET, audience={OWNER: 4})
    env.permissions.revoke(BOB, env.session)
    assert snapshot(env)["grants"][0]["state"] == "inactive"
    assert agent(env, "get", f"/{SHEET}").status_code == 403

    grant(env, kind="file", file=DOC)
    env.connect(OWNER, "token-owner")
    states = {g["file_id"]: g["state"] for g in snapshot(env)["grants"]}
    assert states[DOC] == "disconnected"
    assert agent(env, "get", f"/{DOC}").status_code == 403


def test_delegated_or_unconnected_callers_cannot_grant(env):
    delegated = env.client.post(
        f"/v1/sessions/{env.session}/google-drive/grants",
        json={"kind": "drive", "access": "read", "generation": _generation(env)},
        headers={"x-user": OWNER, "x-delegated": "1"},
    )
    assert delegated.status_code == 403
    env.connections.delete(OWNER)
    stale = env.client.post(
        f"/v1/sessions/{env.session}/google-drive/grants",
        json={"kind": "drive", "access": "read", "generation": "x"},
        headers={"x-user": OWNER},
    )
    assert stale.status_code == 409


def test_grants_belong_to_root_session_only(env):
    grant(env, kind="file", file=DOC)
    assert agent(env, "get", f"/{DOC}", session=env.child).status_code == 200
    assert snapshot(env, session=env.child)["session_id"] == env.session
    assert agent(env, "get", f"/{DOC}", session=env.other).status_code == 403
    assert snapshot(env, session=env.other)["grants"] == []


def test_picker_searches_only_the_callers_drive(env):
    response = env.client.get(
        "/v1/connections/google_drive/files", params={"q": "budget"}, headers={"x-user": BOB}
    )
    assert response.status_code == 200
    assert [f["id"] for f in response.json()["files"]] == [SHEET]
    assert ("search", "token-bob", "budget") in env.drive.calls


async def test_connect_requires_drive_scope(db_uri):
    connections = GoogleDriveConnectionStore(db_uri, SecretBox("test-key"))
    tokens = {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": time.time() + 3600,
        "scope": "openid email",
    }

    class Api:
        async def token(self, fields):
            return dict(tokens)

        async def identity(self, token):
            return {"subject": "s", "email": OWNER}

    hooks = GoogleDriveConnectionHooks(
        GoogleDriveConfig("client", "secret", "https://app.example/callback"), connections, Api()
    )
    with pytest.raises(ConnectionError):
        await hooks.complete(OWNER, "code", {"nonce": "n"})
    with pytest.raises(ConnectionError):
        await hooks.complete("oidc-machine:bot", "code", {"nonce": "n"})
    tokens["scope"] = SCOPES
    await hooks.complete(OWNER, "code", {"nonce": "n"})
    assert connections.get(OWNER).metadata["email"] == OWNER


def test_revoking_during_create_does_not_add_the_new_file(env):
    grant(env, kind="drive", access="write")
    [item] = snapshot(env)["grants"]
    env.drive.on_create = lambda: env.grants.delete(env.session, item["id"])
    response = agent(env, "post", json={"title": "Notes", "kind": "document"})
    assert response.status_code == 200
    assert "not added" in response.json()["warning"]
    assert snapshot(env)["grants"] == []


def test_replacing_a_grant_keeps_one_row_per_scope(env, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from omnigent.stores import google_drive_grant_store

    grant(env, kind="drive", access="write")
    real = google_drive_grant_store.run_write_transaction
    attempts = []

    def flaky(maker, operation, callback):
        attempts.append(operation)
        if len(attempts) == 1:
            raise IntegrityError("insert", {}, Exception("duplicate"))
        return real(maker, operation, callback)

    monkeypatch.setattr(google_drive_grant_store, "run_write_transaction", flaky)
    assert grant(env, kind="drive", access="read").status_code == 200
    assert attempts == ["put", "put"]
    [item] = snapshot(env)["grants"]
    assert item["access"] == "read"
    assert agent(env, "post", f"/{DOC}/append", json={"text": "x"}).status_code == 403


def test_scope_uniqueness_is_enforced_by_the_database(env):
    from sqlalchemy.exc import IntegrityError

    grant(env, kind="drive")
    [item] = env.grants.list(env.session)

    def duplicate(session):
        session.add(
            SqlGoogleDriveGrant(
                workspace_id=0,
                id="f" * 32,
                session_id=env.session,
                user_id=OWNER,
                scope_key="*",
                connection_generation=item.connection_generation,
                kind="drive",
                access="write",
                audience="{}",
                created_at=1,
            )
        )
        session.flush()

    with pytest.raises(IntegrityError):
        env.grants._write_unique("duplicate", duplicate)


def test_group_only_members_can_use_but_not_grant(env):
    team = group_principal("team")
    env.permissions.grant(team, env.session, 2, is_group=True)
    grant(env, kind="file", file=DOC, audience={team: 2})
    groups = {"x-groups": json.dumps(["team"])}
    assert agent(env, "get", f"/{DOC}", user=BOB, headers=groups).status_code == 200
    denied = env.client.post(
        f"/v1/sessions/{env.session}/google-drive/grants",
        json={"kind": "drive", "access": "read", "generation": _generation(env, BOB)},
        headers={"x-user": BOB, **groups},
    )
    assert denied.status_code == 403
    assert "group" in denied.json()["detail"]
    view = env.client.get(
        f"/v1/sessions/{env.session}/google-drive", headers={"x-user": BOB, **groups}
    ).json()
    assert view["viewer"]["can_grant"] is False
    assert view["grants"][0]["state"] == "active"


def test_runner_tokens_cannot_manage_the_drive_connection(env):
    delegated = {"x-user": OWNER, "x-delegated": "1"}
    for method, path in (
        ("post", "/disconnect"),
        ("get", "/connect"),
        ("get", "/status"),
        ("get", "/files"),
    ):
        response = getattr(env.client, method)(
            f"/v1/connections/google_drive{path}", headers=delegated, follow_redirects=False
        )
        assert response.status_code == 403, path
    assert env.connections.get(OWNER) is not None


def test_undecryptable_token_asks_to_reconnect(env, monkeypatch):
    grant(env, kind="file", file=DOC)
    real = env.connections.get

    def without_secret(user, with_tokens=False):
        connection = real(user, with_tokens=with_tokens)
        if with_tokens and connection is not None:
            return replace(connection, secret=None)
        return connection

    monkeypatch.setattr(env.connections, "get", without_secret)
    picker = env.client.get("/v1/connections/google_drive/files", headers={"x-user": OWNER})
    assert picker.status_code == 409
    assert agent(env, "get", f"/{DOC}").status_code == 403


def test_grant_evaluation_reads_sharing_once_per_request(env, monkeypatch):
    for file_id in (DOC, PRIVATE, SHEET):
        grant(env, kind="file", file=file_id)
    calls = []
    real = env.permissions.list_for_session

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(env.permissions, "list_for_session", counted)
    assert len(snapshot(env)["grants"]) == 3
    assert len(calls) == 1
    calls.clear()
    assert agent(env, "get", f"/{DOC}").status_code == 200
    assert len(calls) == 1
