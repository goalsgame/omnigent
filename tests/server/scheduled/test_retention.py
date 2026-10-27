"""Tests for the scheduled-task session retention sweep."""

from __future__ import annotations

import time
import uuid

import pytest

import omnigent.server.scheduled.retention as retention
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.server.scheduled.retention import (
    DEFAULT_SESSION_RETENTION_SECONDS,
    ScheduledSessionRetention,
    parse_session_retention_seconds,
)
from omnigent.stores.conversation_store import PINNED_LABEL_KEY, pinned_label_key
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

RETENTION_S = 3600


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def task_store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    return SqlAlchemyScheduledTaskStore(db_uri)


class _Harness:
    """A sweeper over real stores whose delete goes through ``delete_conversation``."""

    def __init__(
        self,
        task_store: SqlAlchemyScheduledTaskStore,
        conversation_store: SqlAlchemyConversationStore,
    ) -> None:
        self.task_store = task_store
        self.conversation_store = conversation_store
        self.deleted: list[str] = []
        # Items are stamped with the real clock, so runs finish at or after
        # ``base`` and the sweeper's clock is placed well past it.
        self.base = int(time.time())
        self.now = self.base + 2 * RETENTION_S + 1
        self.sweeper = ScheduledSessionRetention(
            store=task_store,
            conversation_store=conversation_store,
            delete_session=self._delete,
            retention_seconds=RETENTION_S,
            clock=lambda: self.now,
        )
        task_store.create(
            scheduled_task_id=_uid("task"),
            name="t",
            prompt="p",
            rrule="FREQ=DAILY",
            user_id=None,
            agent_id=_uid("agent"),
            timezone="UTC",
        )

    async def _delete(self, session_id: str, conv: Conversation) -> bool:
        self.deleted.append(session_id)
        return await self.conversation_store.delete_conversation(session_id)

    def add_run(
        self,
        seed: str,
        *,
        status: str = "succeeded",
        finished_ago: int | None = RETENTION_S + 60,  # seconds before the sweeper clock
        labels: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Create a run and its session; returns ``(run_id, conversation_id)``."""
        conv = self.conversation_store.create_conversation()
        self.conversation_store.append(conv.id, [self._message("user")])
        if labels:
            self.conversation_store.set_labels(conv.id, labels)
        finished_at = None if finished_ago is None else self.now - finished_ago
        run_id = _uid(f"run_{seed}")
        self.task_store.create_run(
            run_id,
            _uid("task"),
            status,
            self.now - 7200,
            conversation_id=conv.id,
            finished_at=finished_at,
        )
        return run_id, conv.id

    @staticmethod
    def _message(role: str) -> NewConversationItem:
        return NewConversationItem(
            type="message",
            response_id="resp_0",
            data=MessageData(
                role=role,
                content=[{"type": "input_text", "text": "hi"}],
                agent="agent" if role == "assistant" else None,
            ),
        )

    def run_conversation_id(self, run_id: str) -> str | None:
        runs, _ = self.task_store.list_runs(_uid("task"))
        return next(r.conversation_id for r in runs if r.id == run_id)


@pytest.fixture()
def harness(
    task_store: SqlAlchemyScheduledTaskStore,
    conversation_store: SqlAlchemyConversationStore,
) -> _Harness:
    return _Harness(task_store, conversation_store)


async def test_deletes_session_of_run_finished_past_retention(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("old")

    assert await harness.sweeper.sweep_once() == 1

    assert harness.deleted == [conv_id]
    assert harness.conversation_store.get_conversation(conv_id) is None
    assert harness.run_conversation_id(run_id) is None


@pytest.mark.parametrize("status", ["succeeded", "failed", "skipped"])
async def test_deletes_for_every_terminal_status(harness: _Harness, status: str) -> None:
    run_id, conv_id = harness.add_run(status, status=status)

    await harness.sweeper.sweep_once()

    assert harness.deleted == [conv_id]
    assert harness.run_conversation_id(run_id) is None


async def test_keeps_recently_finished_session(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("recent", finished_ago=RETENTION_S - 60)

    assert await harness.sweeper.sweep_once() == 0

    assert harness.deleted == []
    assert harness.run_conversation_id(run_id) == conv_id


async def test_keeps_running_run(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("running", status="running", finished_ago=None)

    await harness.sweeper.sweep_once()

    assert harness.deleted == []
    assert harness.run_conversation_id(run_id) == conv_id


async def test_keeps_pinned_session(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("pinned", labels={pinned_label_key("alice"): "1700000000"})

    await harness.sweeper.sweep_once()

    assert harness.deleted == []
    assert harness.run_conversation_id(run_id) == conv_id


async def test_keeps_session_with_canonical_pinned_label(harness: _Harness) -> None:
    _, conv_id = harness.add_run("pinned_canonical", labels={PINNED_LABEL_KEY: "1"})

    await harness.sweeper.sweep_once()

    assert harness.deleted == []
    assert harness.conversation_store.get_conversation(conv_id) is not None


async def test_keeps_session_continued_after_finish(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("continued", finished_ago=harness.now - harness.base)
    time.sleep(1.1)
    harness.conversation_store.append(conv_id, [harness._message("user")])

    await harness.sweeper.sweep_once()

    assert harness.deleted == []
    assert harness.run_conversation_id(run_id) == conv_id


async def test_assistant_message_after_finish_does_not_count_as_engagement(
    harness: _Harness,
) -> None:
    _, conv_id = harness.add_run("assistant_only", finished_ago=harness.now - harness.base)
    time.sleep(1.1)
    harness.conversation_store.append(conv_id, [harness._message("assistant")])

    await harness.sweeper.sweep_once()

    assert harness.deleted == [conv_id]


async def test_clears_run_when_conversation_already_gone(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("gone")
    await harness.conversation_store.delete_conversation(conv_id)

    assert await harness.sweeper.sweep_once() == 0

    assert harness.deleted == []
    assert harness.run_conversation_id(run_id) is None


async def test_clears_task_last_run_pointer(harness: _Harness) -> None:
    _, conv_id = harness.add_run("pointer")
    harness.task_store.update(_uid("task"), last_run_conversation_id=conv_id)

    await harness.sweeper.sweep_once()

    task = harness.task_store.get(_uid("task"))
    assert task is not None
    assert task.last_run_conversation_id is None


async def test_failed_delete_keeps_run_link_and_continues(harness: _Harness) -> None:
    bad_run, bad_conv = harness.add_run("bad")
    good_run, _ = harness.add_run("good")

    async def flaky_delete(session_id: str, conv: Conversation) -> bool:
        if session_id == bad_conv:
            raise RuntimeError("runner exploded")
        return await harness._delete(session_id, conv)

    harness.sweeper._delete_session = flaky_delete

    assert await harness.sweeper.sweep_once() == 1

    assert harness.run_conversation_id(bad_run) == bad_conv
    assert harness.run_conversation_id(good_run) is None


async def test_sweeps_runs_in_every_workspace(
    harness: _Harness,
    task_store: SqlAlchemyScheduledTaskStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    with workspace_scope(7):
        other = _Harness(task_store, conversation_store)
        run_id, conv_id = other.add_run("tenant")

    assert await harness.sweeper.sweep_once() == 1

    with workspace_scope(7):
        assert conversation_store.get_conversation(conv_id) is None
        runs, _ = task_store.list_runs(_uid("task"))
        assert next(r.conversation_id for r in runs if r.id == run_id) is None


async def test_pages_past_kept_sessions(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "_SWEEP_PAGE_SIZE", 2)
    for i in range(3):
        harness.add_run(
            f"pinned_{i}", labels={PINNED_LABEL_KEY: "1"}, finished_ago=RETENTION_S + 300 + i
        )
    _, conv_id = harness.add_run("deletable", finished_ago=RETENTION_S + 100)

    assert await harness.sweeper.sweep_once() == 1

    assert harness.deleted == [conv_id]


def test_sweeper_rejects_non_positive_retention() -> None:
    with pytest.raises(ValueError, match="positive"):
        ScheduledSessionRetention(
            store=None,
            conversation_store=None,
            delete_session=None,  # type: ignore[arg-type]
            retention_seconds=0,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_SESSION_RETENTION_SECONDS),
        ({}, DEFAULT_SESSION_RETENTION_SECONDS),
        ({"session_retention_seconds": 600}, 600),
        ({"session_retention_seconds": 0}, None),
        ({"session_retention_seconds": None}, None),
    ],
)
def test_parse_session_retention_seconds(raw: object, expected: int | None) -> None:
    assert parse_session_retention_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "3600",
        [],
        {"session_retention_seconds": -1},
        {"session_retention_seconds": "1h"},
        {"session_retention_seconds": True},
    ],
)
def test_parse_session_retention_seconds_rejects_malformed(raw: object) -> None:
    with pytest.raises(ValueError, match="scheduled_tasks"):
        parse_session_retention_seconds(raw)
