"""Permission-filtered, ranked session search."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, literal_column, select, text

from omnigent.db.db_models import CONVERSATION_ITEMS_SEARCH_INDEX, SqlConversationItem
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.session_search import (
    TEXT_SEARCH_CONFIG,
    SearchPrincipal,
    _item_type_codes,
    tsvector_expression,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
GROUP = "oidc-group:ZW5naW5lZXJz"


@pytest.fixture(params=["shared", "split"])
def stores(
    request: pytest.FixtureRequest, db_uri: str, tmp_path: Path
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    if request.param == "shared":
        return SqlAlchemyConversationStore(db_uri), SqlAlchemyPermissionStore(db_uri)
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    store = SqlAlchemyConversationStore(omnigent_uri, f"sqlite:///{tmp_path}/conversations.db")
    return store, SqlAlchemyPermissionStore(omnigent_uri)


def _session(
    store: SqlAlchemyConversationStore,
    title: str,
    *messages: str,
    parent: str | None = None,
) -> str:
    conversation = store.create_conversation(title=title, parent_conversation_id=parent)
    if messages:
        store.append(
            conversation.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{index}",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": message}]
                    ),
                )
                for index, message in enumerate(messages)
            ],
        )
    return conversation.id


def _ids(hits: list) -> list[str]:
    return [hit.session_id for hit in hits]


def test_returns_only_sessions_the_caller_can_read(stores) -> None:
    store, perms = stores
    mine = _session(store, "Notes", "the warm pool drained overnight")
    theirs = _session(store, "Bob notes", "warm pool capacity plan")
    shared = _session(store, "Team", "warm pool alerts")
    public = _session(store, "Public", "warm pool dashboard")
    perms.grant(ALICE, mine, 4)
    perms.grant(BOB, theirs, 4)
    perms.grant(BOB, shared, 4)
    perms.grant(GROUP, shared, 1, is_group=True)
    perms.grant(BOB, public, 4)
    perms.grant("__public__", public, 1)

    assert set(_ids(store.search_sessions("warm pool", (ALICE,)))) == {mine}
    assert set(_ids(store.search_sessions("warm pool", (ALICE, GROUP)))) == {mine, shared}


def test_snippets_point_at_matching_messages(stores) -> None:
    store, perms = stores
    session = _session(
        store, "Rollout", "unrelated opening line", "the reconciler patched sleeping workspaces"
    )
    perms.grant(ALICE, session, 4)

    [hit] = store.search_sessions("reconciler", (ALICE,))
    assert hit.session_id == session
    [match] = hit.matches
    assert "reconciler" in match.snippet
    assert match.conversation_id == session


def test_sub_agent_matches_count_for_their_top_level_session(stores) -> None:
    store, perms = stores
    root = _session(store, "Investigation")
    child = _session(store, "worker", "found the checkov failure", parent=root)
    perms.grant(ALICE, root, 4)

    [hit] = store.search_sessions("checkov", (ALICE,))
    assert hit.session_id == root
    assert hit.matches[0].conversation_id == child


def test_title_matches_and_archived_sessions(stores) -> None:
    store, perms = stores
    titled = _session(store, "Atlantis lock cleanup")
    archived = _session(store, "Atlantis archived", "atlantis")
    perms.grant(ALICE, titled, 4)
    perms.grant(ALICE, archived, 4)
    store.update_conversation(archived, archived=True)

    [hit] = store.search_sessions("atlantis", (ALICE,))
    assert hit.session_id == titled
    assert hit.title_match
    assert hit.matches == []


def test_audience_limits_results_to_sessions_everyone_can_read(stores) -> None:
    store, perms = stores
    current = _session(store, "Shared work")
    private = _session(store, "Private", "quarterly budget numbers")
    with_bob = _session(store, "With Bob", "quarterly budget review")
    with_group = _session(store, "With group", "quarterly budget draft")
    for session in (current, private, with_bob, with_group):
        perms.grant(ALICE, session, 4)
    perms.grant(BOB, with_bob, 1)
    perms.grant(GROUP, with_group, 1, is_group=True)

    def search(*audience: SearchPrincipal) -> set[str]:
        return set(
            _ids(
                store.search_sessions(
                    "quarterly budget", (ALICE,), audience=audience, exclude_session_id=current
                )
            )
        )

    alice = SearchPrincipal(ALICE, False)
    assert search(alice) == {private, with_bob, with_group}
    assert search(alice, SearchPrincipal(BOB, False)) == {with_bob}
    assert search(alice, SearchPrincipal(GROUP, True)) == {with_group}
    assert search(alice, SearchPrincipal("__public__", False)) == set()


def test_requires_every_term(stores) -> None:
    store, perms = stores
    both = _session(store, "Both", "terraform plan for the warm pool")
    one = _session(store, "One", "terraform plan only")
    perms.grant(ALICE, both, 4)
    perms.grant(ALICE, one, 4)

    assert _ids(store.search_sessions("terraform pool", (ALICE,))) == [both]
    assert store.search_sessions("   ", (ALICE,)) == []
    assert store.search_sessions("terraform", ()) == []


def test_unauthenticated_single_user_search_skips_acl(stores) -> None:
    store, _ = stores
    session = _session(store, "Local", "local only content")

    assert _ids(store.search_sessions("local", None)) == [session]


def test_postgres_search_uses_the_partial_text_index(conversation_store, db_uri) -> None:
    if not db_uri.startswith("postgresql"):
        pytest.skip("PostgreSQL full-text index")
    query = select(SqlConversationItem.id).where(
        SqlConversationItem.workspace_id == 0,
        SqlConversationItem.type.in_(_item_type_codes()),
        tsvector_expression(SqlConversationItem.search_text).op("@@")(
            func.websearch_to_tsquery(literal_column(f"'{TEXT_SEARCH_CONFIG}'"), "zebrafish")
        ),
    )
    _session(conversation_store, "Bulk", *(f"routine status update {n}" for n in range(2000)))
    _session(conversation_store, "Rare", "a zebrafish appeared")
    with conversation_store._conv_engine.connect() as connection:
        connection.execute(text("ANALYZE conversation_items"))
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": CONVERSATION_ITEMS_SEARCH_INDEX},
        ).scalar_one()
        assert "to_tsvector('english'::regconfig, \"left\"(search_text, 100000))" in definition
        assert "type = ANY (ARRAY[1, 6])" in definition
        compiled = query.compile(connection)
        plan = "\n".join(
            row[0] for row in connection.exec_driver_sql(f"EXPLAIN {compiled}", compiled.params)
        )
    assert CONVERSATION_ITEMS_SEARCH_INDEX in plan


def test_a_long_session_does_not_crowd_out_other_matches(stores) -> None:
    store, perms = stores
    long = _session(store, "Long", *(f"pool pool pool reading {n}" for n in range(450)))
    short = _session(store, "Short", "pool once")
    perms.grant(ALICE, long, 4)
    perms.grant(ALICE, short, 4)

    hits = store.search_sessions("pool", (ALICE,), limit=8)
    assert set(_ids(hits)) == {long, short}
    assert all(len(hit.matches) <= 3 for hit in hits)
