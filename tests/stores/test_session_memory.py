"""Session digests, their entries and semantic session search."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.session_memory import MemoryEntry
from omnigent.stores.conversation_store.session_search import SearchPrincipal
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
DIMENSIONS = 3
MODEL = "test-embedder"


@pytest.fixture(params=["shared", "split"])
def stores(
    request: pytest.FixtureRequest, db_uri: str, tmp_path: Path
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    if request.param == "shared":
        return SqlAlchemyConversationStore(db_uri), SqlAlchemyPermissionStore(db_uri)
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    store = SqlAlchemyConversationStore(omnigent_uri, f"sqlite:///{tmp_path}/conversations.db")
    return store, SqlAlchemyPermissionStore(omnigent_uri)


def _session(store: SqlAlchemyConversationStore, title: str, *messages: str, role="user") -> str:
    conversation = store.create_conversation(title=title)
    if messages:
        store.append(
            conversation.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{index}",
                    data=MessageData(role=role, content=[{"type": "input_text", "text": text}]),
                )
                for index, text in enumerate(messages)
            ],
        )
    return conversation.id


def _remember(store, session_id: str, *entries: MemoryEntry) -> None:
    memory = store.session_memory()
    now = int(time.time())
    assert memory.claim(session_id, now, 600) is not None
    memory.save(
        session_id,
        digest={"summary": "", "topics": [], "entries": []},
        entries=list(entries),
        items_through=0,
        source_updated_at=now,
        model="test-model",
        now=now,
    )


def _later() -> int:
    return int(time.time()) + 3600


def test_quiet_sessions_are_claimed_once_and_digested_progressively(stores) -> None:
    store, _ = stores
    memory = store.session_memory()
    session = _session(store, "Rollout", "should we pin the runner digest?", "yes, pin it")
    empty = _session(store, "Empty")
    child = store.create_conversation(title="worker", parent_conversation_id=session).id

    assert memory.candidates(int(time.time()), 3600, 10) == []
    candidates = {sid for _, sid in memory.candidates(_later(), 60, 10)}
    assert candidates == {session, empty}
    assert child not in candidates

    claimed = memory.claim(session, _later(), 600)
    assert claimed is not None and claimed.status == "pending"
    assert memory.claim(session, _later(), 600) is None
    assert session not in {sid for _, sid in memory.candidates(_later(), 60, 10)}

    items, more = memory.transcript(session, claimed.items_through, 10_000)
    assert [item.text for item in items] == ["should we pin the runner digest?", "yes, pin it"]
    assert [item.role for item in items] == ["user", "user"]
    assert not more
    first, more = memory.transcript(session, claimed.items_through, 5)
    assert len(first) == 1 and more

    _, updated_at = memory.session_state(session)
    memory.save(
        session,
        digest={"summary": "Pinned the runner.", "topics": [], "entries": []},
        entries=[MemoryEntry("decision", "Pin the runner digest", [items[1].item_id])],
        items_through=items[-1].position,
        source_updated_at=updated_at,
        model="test-model",
        now=_later(),
    )
    record = memory.get(session)
    assert record.status == "ready"
    assert record.digest["summary"] == "Pinned the runner."
    assert session not in {sid for _, sid in memory.candidates(_later(), 60, 10)}
    assert memory.transcript(session, record.items_through, 10_000) == ([], False)

    time.sleep(1.1)
    store.append(
        session,
        [
            NewConversationItem(
                type="message",
                response_id="resp_late",
                data=MessageData(
                    role="assistant",
                    agent="helper",
                    content=[{"type": "output_text", "text": "ok"}],
                ),
            )
        ],
    )
    assert session in {sid for _, sid in memory.candidates(_later() + 60, 60, 10)}
    later, _ = memory.transcript(session, record.items_through, 10_000)
    assert [(item.role, item.text) for item in later] == [("assistant", "ok")]


def test_failures_back_off_and_empty_sessions_are_marked(stores) -> None:
    store, _ = stores
    memory = store.session_memory()
    session = _session(store, "Flaky", "hello")
    empty = _session(store, "Empty")
    now = _later()

    memory.claim(session, now, 600)
    memory.fail(session, "Vertex AI returned HTTP 503", now=now, retry_at=now + 300)
    record = memory.get(session)
    assert (record.status, record.attempts, record.error) == (
        "failed",
        1,
        "Vertex AI returned HTTP 503",
    )
    assert memory.claim(session, now + 10, 600) is None
    assert session not in {sid for _, sid in memory.candidates(now + 10, 60, 10)}
    assert session in {sid for _, sid in memory.candidates(now + 301, 60, 10)}

    memory.claim(empty, now, 600)
    _, updated_at = memory.session_state(empty)
    memory.advance(empty, items_through=-1, source_updated_at=updated_at, now=now)
    assert memory.get(empty).status == "empty"
    assert empty not in {sid for _, sid in memory.candidates(now, 60, 10)}


def test_deleting_a_session_removes_its_memory(stores) -> None:
    store, _ = stores
    session = _session(store, "Doomed", "text")
    _remember(store, session, MemoryEntry("decision", "Use gVisor"))

    import asyncio

    assert asyncio.run(store.delete_conversation(session))
    assert store.session_memory().get(session) is None


def test_digest_entries_match_by_word_with_permissions_and_audience(stores) -> None:
    store, perms = stores
    shared = _session(store, "Infra sync", "notes")
    private = _session(store, "Private", "notes")
    for session in (shared, private):
        perms.grant(ALICE, session, 4)
    perms.grant(BOB, shared, 1)
    _remember(
        store,
        shared,
        MemoryEntry("decision", "Run sandboxes under gVisor\nWhy: kernel isolation", ["i1"]),
        MemoryEntry("tangent", "Lunch options"),
    )
    _remember(store, private, MemoryEntry("open_question", "Should gVisor run on Spot nodes?"))

    hits = store.search_sessions("why did we choose gvisor", (ALICE,), memory=True)
    assert {hit.session_id for hit in hits} == {shared, private}
    [first] = [hit for hit in hits if hit.session_id == shared]
    assert [(memory.kind, memory.item_ids) for memory in first.memories] == [("decision", ["i1"])]
    assert first.memories[0].similarity is None

    assert [hit.session_id for hit in store.search_sessions("gvisor", (BOB,), memory=True)] == [
        shared
    ]
    audience = [SearchPrincipal(ALICE, False), SearchPrincipal(BOB, False)]
    assert [
        hit.session_id
        for hit in store.search_sessions("gvisor", (ALICE,), audience=audience, memory=True)
    ] == [shared]


def test_memory_matches_only_when_enabled_and_honour_strict_syntax(stores) -> None:
    store, perms = stores
    session = _session(store, "Infra sync", "notes")
    perms.grant(ALICE, session, 4)
    _remember(
        store,
        session,
        MemoryEntry("decision", "Run sandboxes under gVisor on staging first"),
    )

    assert store.search_sessions("gvisor", (ALICE,)) == []
    assert [hit.session_id for hit in store.search_sessions("gvisor", (ALICE,), memory=True)] == [
        session
    ]
    if store._conv_engine.dialect.name == "postgresql":
        assert store.search_sessions("gvisor -staging", (ALICE,), memory=True) == []
    assert store.search_sessions('"gvisor kata"', (ALICE,), memory=True) == []
    assert store.search_sessions("gvisor kata", (ALICE,), memory=True) != []


def test_a_save_after_the_session_is_deleted_leaves_nothing_behind(stores) -> None:
    import asyncio

    store, _ = stores
    memory = store.session_memory()
    session = _session(store, "Doomed", "text")
    now = int(time.time())
    assert memory.claim(session, now, 600) is not None
    assert asyncio.run(store.delete_conversation(session))

    memory.save(
        session,
        digest={"summary": "late", "topics": [], "entries": []},
        entries=[MemoryEntry("decision", "late entry")],
        items_through=0,
        source_updated_at=now,
        model="test-model",
        now=now,
    )
    assert memory.get(session) is None
    assert store.search_sessions("late", None, memory=True) == []


def test_postgres_widens_questions_that_match_no_single_message(conversation_store) -> None:
    if conversation_store._conv_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL text search")
    session = _session(conversation_store, "Capacity", "we sized the warm pool at five spares")
    other = _session(conversation_store, "Unrelated", "lunch is at noon")

    assert conversation_store.search_sessions("warm pool", None)[0].session_id == session
    hits = conversation_store.search_sessions("how big should the warm pool be", None)
    assert [hit.session_id for hit in hits] == [session]
    assert "warm pool" in hits[0].matches[0].snippet
    assert conversation_store.search_sessions('"warm pool" big', None) == []
    assert other not in {hit.session_id for hit in hits}


def test_postgres_full_matches_outrank_sessions_with_more_partial_matches(
    conversation_store,
) -> None:
    store = conversation_store
    if store._conv_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL text search")
    full = _session(store, "Full", "the warm pool drained")
    partial = _session(store, "Partial", "pool one", "pool two", "pool three")

    hits = store.search_sessions("warm pool", None)
    assert [hit.session_id for hit in hits] == [full, partial]


def test_postgres_semantic_matches_respect_similarity_and_permissions(stores) -> None:
    store, permission_store = stores
    if store._conv_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL with pgvector")
    store.ensure_session_memory_vectors(DIMENSIONS, MODEL)
    store.ensure_session_memory_vectors(DIMENSIONS, MODEL)
    near = _session(store, "Sandbox isolation", "notes")
    far = _session(store, "Budget", "notes")
    hidden = _session(store, "Hidden", "notes")
    for session in (near, far):
        permission_store.grant(ALICE, session, 4)
    permission_store.grant(BOB, hidden, 4)
    _remember(store, near, MemoryEntry("decision", "Run agents in a userspace kernel"))
    _remember(store, far, MemoryEntry("discussion", "Quarterly spend review"))
    _remember(store, hidden, MemoryEntry("decision", "Run agents in microVMs"))
    memory = store.session_memory()
    vectors = {
        "Run agents in a userspace kernel": [1.0, 0.1, 0.0],
        "Quarterly spend review": [0.0, 0.0, 1.0],
        "Run agents in microVMs": [1.0, 0.0, 0.0],
    }
    pending = memory.unembedded(10)
    assert {text for _, _, text in pending} == set(vectors)
    for workspace, entry_id, text in pending:
        memory.set_embedding(workspace, entry_id, vectors[text])
    assert memory.unembedded(10) == []

    hits = store.search_sessions(
        "isolation approach",
        (ALICE,),
        query_embedding=[1.0, 0.0, 0.0],
        min_similarity=0.5,
        memory=True,
    )
    assert [hit.session_id for hit in hits] == [near]
    assert hits[0].memories[0].similarity > 0.9

    loose = store.search_sessions(
        "isolation approach",
        (ALICE,),
        query_embedding=[1.0, 0.0, 0.0],
        min_similarity=0.0,
        memory=True,
    )
    assert [hit.session_id for hit in loose] == [near, far]

    assert (
        store.search_sessions(
            '"userspace kernel" -agents', (ALICE,), query_embedding=[1.0, 0.0, 0.0], memory=True
        )
        == []
    )

    store.ensure_session_memory_vectors(DIMENSIONS, MODEL)
    assert memory.unembedded(10) == []
    store.ensure_session_memory_vectors(DIMENSIONS, "another-model")
    assert len(memory.unembedded(10)) == 3
    for workspace, entry_id, text in memory.unembedded(10):
        memory.set_embedding(workspace, entry_id, vectors[text])
    store.ensure_session_memory_vectors(DIMENSIONS + 1, "another-model")
    assert len(memory.unembedded(10)) == 3
