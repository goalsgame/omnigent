"""Session search REST routes."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import (
    bind_group_authority,
    group_authority_scope,
    group_principal,
)
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.session_search import create_session_search_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

OWNER = "owner@example.test"
BOB = "bob@example.test"
TEAM = "engineers"


class _Auth(AuthProvider):
    def get_user_id(self, request):
        user = request.headers.get("x-user")
        if user:
            bind_group_authority(
                user, json.loads(request.headers.get("x-groups", "[]")), int(time.time()) + 60
            )
        return user


def _session(conversations, title, text=None, parent=None):
    session = conversations.create_conversation(title=title, parent_conversation_id=parent).id
    if text:
        conversations.append(
            session,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_1",
                    data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
                )
            ],
        )
    return session


@pytest.fixture
def env(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, BOB):
        permissions.ensure_user(user)
    current = _session(conversations, "Current work", "deploy notes for the gateway")
    child = _session(conversations, "worker", parent=current)
    private = _session(conversations, "Private", "gateway deploy checklist")
    team = _session(conversations, "Team", "gateway deploy postmortem")
    for session in (current, private, team):
        permissions.grant(OWNER, session, 4)
    permissions.grant(BOB, current, 2)
    permissions.grant(BOB, team, 1)
    permissions.grant(group_principal(TEAM), private, 1, is_group=True)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with account_authority_scope(None, None), group_authority_scope(), workspace_scope(0):
            return await call_next(request)

    app.include_router(
        create_session_search_router(conversations, permissions, _Auth()), prefix="/v1"
    )
    return SimpleNamespace(
        client=TestClient(app),
        current=current,
        child=child,
        private=private,
        team=team,
    )


def _ids(response):
    assert response.status_code == 200, response.text
    return {hit["session_id"] for hit in response.json()["data"]}


def test_people_search_their_readable_sessions(env):
    def search(user, groups=()):
        return env.client.get(
            "/v1/sessions/search",
            params={"q": "gateway deploy"},
            headers={"x-user": user, "x-groups": json.dumps(list(groups))},
        )

    assert _ids(search(OWNER)) == {env.current, env.private, env.team}
    assert _ids(search(BOB)) == {env.current, env.team}
    assert _ids(search(BOB, [TEAM])) == {env.current, env.private, env.team}

    [hit] = [hit for hit in search(OWNER).json()["data"] if hit["session_id"] == env.team]
    assert hit["title"] == "Team"
    assert "postmortem" in hit["matches"][0]["snippet"]


def test_search_requires_a_user_and_a_query(env):
    assert env.client.get("/v1/sessions/search", params={"q": "gateway"}).status_code == 401
    blank = env.client.get("/v1/sessions/search", params={"q": " "}, headers={"x-user": OWNER})
    assert blank.status_code == 400


def test_agent_search_is_limited_to_what_the_whole_session_can_read(env):
    def agent(session):
        return env.client.get(
            f"/v1/sessions/{session}/search",
            params={"q": "gateway deploy"},
            headers={"x-user": OWNER},
        )

    assert _ids(agent(env.current)) == {env.team}
    assert _ids(agent(env.child)) == {env.team}


def test_agent_search_requires_access_to_the_session(env):
    response = env.client.get(
        f"/v1/sessions/{env.private}/search",
        params={"q": "gateway"},
        headers={"x-user": BOB},
    )
    assert response.status_code == 404
