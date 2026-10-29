"""An enabled agent searches past sessions over HTTP, limited to what its audience can read."""

import asyncio
import json
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import group_authority_scope
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.runner.session_search import execute_session_search_tool
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.session_search import create_session_search_router
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.tools.builtins.session_search import SearchSessionsTool
from omnigent.tools.manager import ToolManager

OWNER = "owner@example.com"
PEER = "peer@example.com"


class UserAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("X-Test-User")


def _session(conversations, title, text):
    session = conversations.create_conversation(title=title).id
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


def test_agent_search_over_http_returns_only_sessions_the_audience_can_read(db_uri, monkeypatch):
    monkeypatch.setenv("OMNIGENT_SESSION_SEARCH_TOOL", "1")
    tools = ToolManager(AgentSpec(spec_version=1), os_env_schema_only=True).get_tool_schemas()
    assert SearchSessionsTool.name() in {tool["function"]["name"] for tool in tools}

    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, PEER):
        permissions.ensure_user(user)
    current = _session(conversations, "Current", "planning the next release")
    shared = _session(conversations, "Postmortem", "the gateway certificate expired overnight")
    private = _session(conversations, "Private", "gateway certificate renewal costs")
    for session in (current, shared, private):
        permissions.grant(OWNER, session, 4)
    permissions.grant(PEER, current, 2)
    permissions.grant(PEER, shared, 1)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with account_authority_scope(None, None), group_authority_scope(), workspace_scope(0):
            return await call_next(request)

    app.include_router(
        create_session_search_router(conversations, permissions, UserAuth()), prefix="/v1"
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

            async def agent(args):
                async with httpx.AsyncClient(
                    base_url=address, headers={"X-Test-User": OWNER}
                ) as runner:
                    return json.loads(await execute_session_search_tool(args, current, runner))

            result = asyncio.run(agent({"query": "gateway certificate"}))
            [hit] = result["sessions"]
            assert hit["session_id"] == shared
            assert "certificate expired" in hit["matches"][0]["snippet"]

            with httpx.Client(base_url=address, headers={"X-Test-User": OWNER}) as person:
                found = person.get("/v1/sessions/search", params={"q": "gateway certificate"})
            assert {hit["session_id"] for hit in found.json()["data"]} == {shared, private}
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
