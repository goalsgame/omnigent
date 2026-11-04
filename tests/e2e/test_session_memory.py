"""A configured server digests a quiet session and finds its decision by meaning over HTTP."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server import app as server_app
from omnigent.server import session_memory
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.test_app import _independent_seed_stores
from tests.server.test_session_memory import RAW_CONFIG, FakeVertex, _Credential


async def test_quiet_session_is_digested_embedded_and_found_by_meaning(
    db_uri, tmp_path, monkeypatch
) -> None:
    if not db_uri.startswith("postgresql"):
        pytest.skip("Requires PostgreSQL with pgvector")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "true")
    for name in (
        "_ensure_default_native_agents",
        "_ensure_default_acp_agents",
        "_ensure_default_debby_agent",
        "_ensure_default_polly_agent",
    ):
        monkeypatch.setattr(server_app, name, lambda *args: None)

    vertex = FakeVertex()
    vertex.digest = {
        "summary": "Agents run under gVisor.",
        "topics": ["isolation"],
        "entries": [
            {
                "kind": "decision",
                "title": "Use gVisor",
                "text": "Sandboxes run under gVisor",
                "rationale": "Kernel isolation without nested virtualization",
                "alternatives": ["Kata"],
                "refs": [1, 2],
            }
        ],
    }
    original = session_memory.SessionMemory.__init__

    def with_fake_vertex(self, config, conversations, **_kwargs) -> None:
        original(
            self,
            config,
            conversations,
            credential=_Credential(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(vertex)),
            clock=lambda: time.time() + 3600,
        )

    monkeypatch.setattr(session_memory.SessionMemory, "__init__", with_fake_vertex)

    stores = _independent_seed_stores(tmp_path, "memory")
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(title="Sandbox isolation").id
    conversations.append(
        session,
        [
            NewConversationItem(
                type="message",
                response_id=f"resp_{index}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for index, text in enumerate(
                ["Should sandboxes use gVisor or Kata?", "Go with gVisor, Kata needs nested virt."]
            )
        ],
    )

    from omnigent.runtime import init as init_runtime

    file_store = SqlAlchemyFileStore(db_uri)
    init_runtime(
        agent_store=stores.agent_store,
        conversation_store=conversations,
        agent_cache=stores.agent_cache,
        file_store=file_store,
        artifact_store=stores.artifact_store,
    )
    app = server_app.create_app(
        agent_store=stores.agent_store,
        artifact_store=stores.artifact_store,
        conversation_store=conversations,
        file_store=file_store,
        agent_cache=stores.agent_cache,
        server_config={"session_memory": RAW_CONFIG},
    )
    async with app.router.lifespan_context(app):
        memory = conversations.session_memory()
        deadline = time.monotonic() + 20
        while True:
            record = memory.get(session)
            if record is not None and record.status == "ready" and not memory.unembedded(10):
                break
            assert time.monotonic() < deadline, "session was not digested and embedded"
            await asyncio.sleep(0.2)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            digest = (await client.get(f"/v1/sessions/{session}/digest")).json()
            found = await client.get(
                "/v1/sessions/search", params={"q": "which isolation runtime for gvisor sandboxes"}
            )

    assert digest["status"] == "ready"
    [decision] = digest["entries"]
    assert decision["alternatives"] == ["Kata"]
    assert len(decision["item_ids"]) == 2
    assert found.status_code == 200, found.text
    hit = found.json()["data"][0]
    assert hit["session_id"] == session
    [memory_match] = [match for match in hit["memories"] if match["kind"] == "decision"]
    assert memory_match["similarity"] > 0.9
    assert ("RETRIEVAL_QUERY", "which isolation runtime for gvisor sandboxes") in vertex.embedded
