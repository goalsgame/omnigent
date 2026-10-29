"""Session digests written by a Vertex model and served with session search."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import group_authority_scope
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.session_search import create_session_search_router
from omnigent.server.session_memory import (
    SessionMemory,
    SessionMemoryConfig,
    build_digest,
    render_transcript,
)
from omnigent.stores.conversation_store.session_memory import TranscriptItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

OWNER = "owner@example.test"
OTHER = "other@example.test"
RAW_CONFIG = {
    "digest": {"project": "example-project", "location": "eu", "model": "gemini-flash"},
    "embeddings": {
        "project": "example-project",
        "location": "europe-west1",
        "model": "text-embedder",
        "dimensions": 3,
    },
    "idle_minutes": 1,
    "min_similarity": 0.6,
}


class _Auth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-user")


class _Credential:
    async def credential(self) -> str:
        return "token"


class FakeVertex:
    """Answers digest and embedding requests like Vertex AI."""

    def __init__(self) -> None:
        self.digest: dict | None = None
        self.digest_status = 200
        self.embed_status = 200
        self.prompts: list[str] = []
        self.embedded: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        body = json.loads(request.content)
        if request.url.path.endswith("/gemini-flash:generateContent"):
            assert request.url.host == "aiplatform.eu.rep.googleapis.com"
            self.prompts.append(body["contents"][0]["parts"][0]["text"])
            text = json.dumps(self.digest)
            return httpx.Response(
                self.digest_status,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": "thinking", "thought": True}, {"text": text}]
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        assert request.url.host == "europe-west1-aiplatform.googleapis.com"
        assert request.url.path.endswith("/text-embedder:embedContent")
        assert body["outputDimensionality"] == 3
        assert body["autoTruncate"] is True
        content = body["content"]["parts"][0]["text"]
        self.embedded.append((body["taskType"], content))
        vector = [1.0, 0.0, 0.0] if "gvisor" in content.lower() else [0.0, 1.0, 0.0]
        return httpx.Response(self.embed_status, json={"embedding": {"values": vector}})


def _items(*texts: str) -> list[TranscriptItem]:
    return [
        TranscriptItem(
            item_id=f"{index:032x}", position=index, created_at=100 + index, role="user", text=text
        )
        for index, text in enumerate(texts, start=1)
    ]


def _memory(store, vertex: FakeVertex, now: float) -> SessionMemory:
    return SessionMemory(
        SessionMemoryConfig.parse(RAW_CONFIG),
        store,
        credential=_Credential(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(vertex)),
        clock=lambda: now,
    )


def _session(store, title: str, *texts: str) -> str:
    session = store.create_conversation(title=title).id
    store.append(
        session,
        [
            NewConversationItem(
                type="message",
                response_id=f"resp_{index}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for index, text in enumerate(texts)
        ],
    )
    return session


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"digest": None}, "digest and embeddings"),
        ({"extra": 1}, "digest and embeddings"),
        ({"digest": {"project": "example-project", "location": "eu"}}, "project, location"),
        ({"digest": {**RAW_CONFIG["digest"], "model": "a/b"}}, "model"),
        ({"digest": {**RAW_CONFIG["digest"], "location": "mars"}}, "location"),
        ({"embeddings": {**RAW_CONFIG["embeddings"], "dimensions": 4096}}, "dimensions"),
        ({"idle_minutes": 0}, "idle_minutes"),
        ({"min_similarity": 1}, "min_similarity"),
    ],
)
def test_config_rejects_invalid_settings(change, message) -> None:
    raw = {**RAW_CONFIG, **change}
    raw = {key: value for key, value in raw.items() if value is not None}
    with pytest.raises(ValueError, match=message):
        SessionMemoryConfig.parse(raw)


def test_config_defaults() -> None:
    assert SessionMemoryConfig.parse(None) is None
    config = SessionMemoryConfig.parse(
        {
            "digest": RAW_CONFIG["digest"],
            "embeddings": {
                key: value
                for key, value in RAW_CONFIG["embeddings"].items()
                if key != "dimensions"
            },
        }
    )
    assert (config.dimensions, config.idle_minutes, config.min_similarity) == (768, 15, 0.6)
    assert config.digest.url("generateContent") == (
        "https://aiplatform.eu.rep.googleapis.com/v1/projects/example-project/locations/eu"
        "/publishers/google/models/gemini-flash:generateContent"
    )


def test_digest_cites_messages_and_carries_previous_entries_forward() -> None:
    items = _items("should we use gvisor?", "yes, gvisor it is", "what about spot nodes?")
    previous = {
        "summary": "old",
        "topics": [],
        "entries": [
            {
                "kind": "open_question",
                "title": "Isolation",
                "text": "gVisor or Kata?",
                "item_ids": ["aa"],
                "message_ids": ["m-aa"],
                "created_at": 50,
            }
        ],
    }
    prompt = render_transcript("Sandboxes", previous, items)
    assert '"id": "p1"' in prompt
    assert "[3] user: what about spot nodes?" in prompt

    digest, entries = build_digest(
        {
            "summary": "Chose  gVisor.",
            "topics": ["isolation", 7],
            "entries": [
                {
                    "kind": "decision",
                    "title": "Use gVisor",
                    "text": "Sandboxes run under gVisor",
                    "rationale": "Kernel isolation",
                    "alternatives": ["Kata"],
                    "refs": [2, 2, 9, "1"],
                    "from": ["p1", "p7"],
                },
                {"kind": "open_question", "title": "Spot", "text": "Spot nodes?", "refs": [3]},
                {"kind": "gossip", "title": "x", "text": "y", "refs": []},
                {"kind": "tangent", "title": "", "text": "", "refs": []},
            ],
        },
        previous,
        items,
        now=999,
    )
    assert digest["summary"] == "Chose gVisor."
    assert digest["topics"] == ["isolation"]
    decision, question = digest["entries"]
    assert decision["item_ids"] == ["aa", items[1].item_id]
    assert decision["message_ids"] == ["m-aa", items[1].item_id]
    assert decision["created_at"] == items[1].created_at
    assert decision["alternatives"] == ["Kata"]
    assert question["item_ids"] == [items[2].item_id]
    assert [entry.kind for entry in entries] == ["summary", "decision", "open_question"]
    assert (
        entries[1].text
        == "Use gVisor: Sandboxes run under gVisor\nWhy: Kernel isolation\nRejected: Kata"
    )
    assert entries[0].text == "Chose gVisor.\nTopics: isolation"

    reply = TranscriptItem("b" * 32, 9, 300, "assistant", "agreed", response_id="resp_9")
    summary = TranscriptItem("c" * 32, 10, 301, "summary", "earlier work")
    linked, _ = build_digest(
        {
            "summary": "",
            "topics": [],
            "entries": [{"kind": "discussion", "title": "t", "text": "x", "refs": [1, 2]}],
        },
        None,
        [reply, summary],
        now=1,
    )
    assert linked["entries"][0]["item_ids"] == ["b" * 32, "c" * 32]
    assert linked["entries"][0]["message_ids"] == ["resp_9"]

    with pytest.raises(ValueError):
        build_digest({"summary": "x"}, None, items, now=1)


async def test_worker_digests_quiet_sessions_embeds_entries_and_backs_off(db_uri) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session = _session(store, "Sandboxes", "should agents run under gvisor?", "yes")
    vertex = FakeVertex()
    vertex.digest = {
        "summary": "Agents run under gVisor.",
        "topics": ["isolation"],
        "entries": [{"kind": "decision", "title": "gVisor", "text": "Use gVisor", "refs": [1]}],
    }
    now = time.time() + 3600
    memory = _memory(store, vertex, now)

    assert await memory.tick() is False
    record = store.session_memory().get(session)
    assert record.status == "ready"
    assert record.model == "gemini-flash"
    assert record.digest["entries"][0]["title"] == "gVisor"
    assert "[1] user: should agents run under gvisor?" in vertex.prompts[0]
    assert vertex.embedded == []

    assert await memory.process(session) is True
    assert len(vertex.prompts) == 1

    later = _session(store, "Budget", "spend review")
    vertex.digest_status = 503
    await memory.tick()
    failed = store.session_memory().get(later)
    assert (failed.status, failed.attempts) == ("failed", 1)
    assert failed.error == "Vertex AI returned HTTP 503"
    assert await memory.process(later) is False
    await memory.aclose()


async def test_worker_embeds_entries_on_postgres(db_uri) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    if store._conv_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL with pgvector")
    session = _session(store, "Sandboxes", "should agents run under gvisor?")
    vertex = FakeVertex()
    vertex.digest = {
        "summary": "Agents run under gVisor.",
        "topics": [],
        "entries": [{"kind": "decision", "title": "gVisor", "text": "Use gVisor", "refs": [1]}],
    }
    memory = _memory(store, vertex, time.time() + 3600)
    assert await memory.embed_query("gvisor") is None
    await memory.prepare()

    vertex.embed_status = 500
    await memory.tick()
    assert len(store.session_memory().unembedded(10)) == 2
    vertex.embed_status = 200
    await memory.tick()
    assert store.session_memory().unembedded(10) == []
    assert {task for task, _ in vertex.embedded} == {"RETRIEVAL_DOCUMENT"}

    assert await memory.embed_query("which isolation did we choose for gvisor") == [1.0, 0.0, 0.0]
    vertex.embed_status = 500
    assert await memory.embed_query("anything") is None
    hits = store.search_sessions(
        "isolation choice", None, query_embedding=[1.0, 0.0, 0.0], min_similarity=0.6
    )
    assert [hit.session_id for hit in hits] == [session]
    await memory.aclose()


@pytest.fixture
def api(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, OTHER):
        permissions.ensure_user(user)
    vertex = FakeVertex()
    memory = _memory(store, vertex, time.time() + 3600)

    def build(with_memory: bool) -> TestClient:
        app = FastAPI()

        @app.exception_handler(OmnigentError)
        async def handle_error(request, exc):
            return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

        @app.middleware("http")
        async def authority(request: Request, call_next):
            with account_authority_scope(None, None), group_authority_scope(), workspace_scope(0):
                return await call_next(request)

        app.include_router(
            create_session_search_router(
                store, permissions, _Auth(), memory if with_memory else None
            ),
            prefix="/v1",
        )
        return TestClient(app)

    return store, permissions, vertex, memory, build


def test_digest_route_requires_read_access_and_reports_progress(api) -> None:
    import asyncio

    store, permissions, vertex, memory, build = api
    session = _session(store, "Sandboxes", "should agents run under gvisor?")
    child = store.create_conversation(title="worker", parent_conversation_id=session).id
    permissions.grant(OWNER, session, 4)
    client = build(True)

    def digest(session_id: str, user: str = OWNER):
        return client.get(f"/v1/sessions/{session_id}/digest", headers={"x-user": user})

    assert digest(session).json()["status"] == "pending"
    assert digest(session, OTHER).status_code == 404
    assert (
        build(False).get(f"/v1/sessions/{session}/digest", headers={"x-user": OWNER}).status_code
        == 404
    )

    vertex.digest = {
        "summary": "Agents run under gVisor.",
        "topics": ["isolation"],
        "entries": [{"kind": "decision", "title": "gVisor", "text": "Use gVisor", "refs": [1]}],
    }
    asyncio.run(memory.process(session))
    body = digest(child).json()
    assert body["session_id"] == session
    assert body["status"] == "ready"
    assert body["summary"] == "Agents run under gVisor."
    assert body["entries"][0]["kind"] == "decision"
    assert body["error"] is None

    found = client.get(
        "/v1/sessions/search", params={"q": "gvisor decision"}, headers={"x-user": OWNER}
    ).json()["data"]
    assert found[0]["session_id"] == session
    assert any(memory["kind"] == "decision" for memory in found[0]["memories"])


SERVER_CONFIG = {"session_memory": RAW_CONFIG}


async def test_app_serves_digests_when_configured(db_uri, tmp_path, monkeypatch) -> None:
    from tests.server.routes.test_sessions_sharing_mode import _build_app

    monkeypatch.setattr("omnigent.server.server_config.load_server_config", lambda: SERVER_CONFIG)
    app = _build_app(db_uri, tmp_path)
    session = SqlAlchemyConversationStore(db_uri).create_conversation().id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/sessions/{session}/digest")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"

    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"session_memory": {"digest": RAW_CONFIG["digest"]}},
    )
    with pytest.raises(ValueError, match="session_memory"):
        _build_app(db_uri, tmp_path)


async def test_app_startup_prepares_vectors_and_runs_the_worker(
    runtime_init, db_uri, tmp_path, monkeypatch
) -> None:
    from tests.server.routes.test_sessions_sharing_mode import _build_app

    if not db_uri.startswith("postgresql"):
        pytest.skip("Requires PostgreSQL with pgvector")
    started = []

    async def run(self) -> None:
        started.append(self._vectors)

    monkeypatch.setattr(SessionMemory, "run", run)
    monkeypatch.setattr("omnigent.server.server_config.load_server_config", lambda: SERVER_CONFIG)
    app = _build_app(db_uri, tmp_path)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert started == [True]
    assert SqlAlchemyConversationStore(db_uri).session_memory().unembedded(1) == []
