"""Tests for the guarded path of ``purge_session`` (row deleted before teardown)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY
from omnigent.server.routes._sessions import orchestration as sessions_orchestration
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.server.routes._sessions.session_purge import purge_session
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


async def test_guarded_purge_uses_pre_delete_snapshot(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(runner_id="runner_root")
    child = store.create_conversation(parent_conversation_id=root.id, runner_id="runner_child")
    store.set_labels(root.id, {BRIDGE_ID_LABEL_KEY: "rotated-bridge", "other": "x"})
    root_row = store.get_conversation(root.id)
    assert root_row is not None
    _session_status_cache[child.id] = "running"

    deletes: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        deletes.append(request)
        return httpx.Response(200, json={})

    stop_calls: list[tuple[str, Any]] = []

    async def fake_stop(target_id: str, router: Any, *, conversation: Any = None) -> bool:
        stop_calls.append((target_id, conversation))
        return True

    async def fake_runner_client(session_id: str, **kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["conversation"].id == session_id
        return httpx.AsyncClient(transport=httpx.MockTransport(dispatch), base_url="http://runner")

    with (
        patch.object(sessions_orchestration, "_stop_session_via_runner", fake_stop),
        patch(
            "omnigent.server.routes._sessions.session_purge._get_runner_client_for_resource_access",
            fake_runner_client,
        ),
    ):
        outcome = await purge_session(
            root.id,
            root_row,
            conversation_store=store,
            runner_router=None,
            file_store=None,
            artifact_store=None,
            app_state=SimpleNamespace(),
            unengaged_since=1,
        )

    assert outcome == "deleted"
    assert store.get_conversation(root.id) is None
    assert store.get_conversation(child.id) is None
    assert [(target, conv.runner_id) for target, conv in stop_calls] == [
        (child.id, "runner_child")
    ]
    (delete,) = deletes
    assert delete.method == "DELETE"
    assert json.loads(delete.content) == {
        "cleanup_labels": {BRIDGE_ID_LABEL_KEY: "rotated-bridge"}
    }
    _session_status_cache.pop(child.id, None)


async def test_guarded_purge_keeps_engaged_session_untouched(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation()
    store.set_labels(root.id, {"omnigent.pinned.local": "1"})
    root_row = store.get_conversation(root.id)
    assert root_row is not None
    stop = AsyncMock()

    with patch.object(sessions_orchestration, "_stop_session_via_runner", stop):
        outcome = await purge_session(
            root.id,
            root_row,
            conversation_store=store,
            runner_router=None,
            file_store=None,
            artifact_store=None,
            app_state=SimpleNamespace(),
            unengaged_since=1,
        )

    assert outcome == "engaged"
    assert store.get_conversation(root.id) is not None
    stop.assert_not_called()


def _app_state(*, local_hosts: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        host_registry=SimpleNamespace(
            get=lambda host_id: host_id if host_id in local_hosts else None
        ),
        host_store=SimpleNamespace(
            get_host=lambda host_id: SimpleNamespace(id=host_id, sandbox_provider=None)
        ),
    )


async def _purge(store: SqlAlchemyConversationStore, conv_id: str, app_state: Any) -> str:
    row = store.get_conversation(conv_id)
    assert row is not None
    with (
        patch("omnigent.server.routes.sessions.routes_events.host_is_live", return_value=True),
        patch.object(sessions_orchestration, "_stop_session_via_runner", AsyncMock()),
        patch(
            "omnigent.server.routes._sessions.session_purge._get_runner_client_for_resource_access",
            AsyncMock(return_value=None),
        ),
    ):
        return await purge_session(
            conv_id,
            row,
            conversation_store=store,
            runner_router=None,
            file_store=None,
            artifact_store=None,
            app_state=app_state,
            unengaged_since=1,
        )


async def test_guarded_purge_skips_session_whose_runner_is_on_another_replica(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(host_id="b" * 32, workspace="/w", runner_id="runner_remote")

    outcome = await _purge(store, root.id, _app_state(local_hosts=set()))

    assert outcome == "other_replica"
    assert store.get_conversation(root.id) is not None


async def test_guarded_purge_skips_when_only_a_descendant_runner_is_remote(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(host_id="a" * 32, workspace="/w", runner_id="runner_local")
    child = store.create_conversation(
        parent_conversation_id=root.id, host_id="b" * 32, workspace="/w", runner_id="runner_remote"
    )

    outcome = await _purge(store, root.id, _app_state(local_hosts={"a" * 32}))

    assert outcome == "other_replica"
    assert store.get_conversation(child.id) is not None


async def test_guarded_purge_proceeds_when_runner_is_on_this_replica(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(host_id="a" * 32, workspace="/w", runner_id="runner_local")

    outcome = await _purge(store, root.id, _app_state(local_hosts={"a" * 32}))

    assert outcome == "deleted"
    assert store.get_conversation(root.id) is None


async def test_guarded_purge_proceeds_without_a_runner_binding(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation()

    outcome = await _purge(store, root.id, _app_state(local_hosts=set()))

    assert outcome == "deleted"
