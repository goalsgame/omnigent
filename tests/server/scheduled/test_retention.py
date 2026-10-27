"""Tests for the scheduled-task session retention sweep."""

from __future__ import annotations

import time
import uuid
from typing import Literal

import pytest

import omnigent.server.scheduled.retention as retention
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.server.scheduled.retention import (
    DEFAULT_SESSION_RETENTION_SECONDS,
    ScheduledSessionRetention,
    parse_scheduled_tasks_config,
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

    async def _delete(
        self, session_id: str, conv: Conversation, finished_at: int
    ) -> Literal["deleted", "engaged", "missing"]:
        outcome = await self.conversation_store.delete_conversation_if_unengaged(
            session_id, finished_at
        )
        if outcome == "deleted":
            self.deleted.append(session_id)
        return outcome

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

    async def flaky_delete(
        session_id: str, conv: Conversation, finished_at: int
    ) -> Literal["deleted", "engaged", "missing"]:
        if session_id == bad_conv:
            raise RuntimeError("runner exploded")
        return await harness._delete(session_id, conv, finished_at)

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


async def test_message_arriving_during_delete_keeps_session(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("race", finished_ago=harness.now - harness.base)
    time.sleep(1.1)
    real_delete = harness.sweeper._delete_session

    async def delete_after_user_message(
        session_id: str, conv: Conversation, finished_at: int
    ) -> Literal["deleted", "engaged", "missing"]:
        harness.conversation_store.append(conv_id, [harness._message("user")])
        return await real_delete(session_id, conv, finished_at)

    harness.sweeper._delete_session = delete_after_user_message

    assert await harness.sweeper.sweep_once() == 0

    assert harness.conversation_store.get_conversation(conv_id) is not None
    assert harness.run_conversation_id(run_id) == conv_id


async def test_deletes_session_with_unprotected_descendants(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("tree")
    child = harness.conversation_store.create_conversation(parent_conversation_id=conv_id)

    assert await harness.sweeper.sweep_once() == 1

    assert harness.conversation_store.get_conversation(child.id) is None
    assert harness.run_conversation_id(run_id) is None


async def test_keeps_everything_when_a_descendant_is_pinned(harness: _Harness) -> None:
    run_id, conv_id = harness.add_run("pinned_child")
    child = harness.conversation_store.create_conversation(parent_conversation_id=conv_id)
    grandchild = harness.conversation_store.create_conversation(parent_conversation_id=child.id)
    harness.conversation_store.set_labels(grandchild.id, {pinned_label_key("alice"): "1"})

    assert await harness.sweeper.sweep_once() == 0

    assert harness.conversation_store.get_conversation(conv_id) is not None
    assert harness.conversation_store.get_conversation(grandchild.id) is not None
    assert harness.run_conversation_id(run_id) == conv_id


async def test_keeps_everything_when_a_descendant_has_a_later_user_message(
    harness: _Harness,
) -> None:
    run_id, conv_id = harness.add_run("continued_child", finished_ago=harness.now - harness.base)
    child = harness.conversation_store.create_conversation(parent_conversation_id=conv_id)
    time.sleep(1.1)
    harness.conversation_store.append(child.id, [harness._message("user")])

    assert await harness.sweeper.sweep_once() == 0

    assert harness.conversation_store.get_conversation(child.id) is not None
    assert harness.run_conversation_id(run_id) == conv_id


async def test_user_message_followed_by_many_assistant_messages_is_still_engagement(
    harness: _Harness,
) -> None:
    run_id, conv_id = harness.add_run("chatty", finished_ago=harness.now - harness.base)
    time.sleep(1.1)
    harness.conversation_store.append(conv_id, [harness._message("user")])
    harness.conversation_store.append(conv_id, [harness._message("assistant")] * 60)

    assert await harness.sweeper.sweep_once() == 0

    assert harness.run_conversation_id(run_id) == conv_id


async def test_task_pointer_to_newer_session_is_left_alone(harness: _Harness) -> None:
    _, old_conv = harness.add_run("old_run")
    _, newer_conv = harness.add_run("newer_run", finished_ago=RETENTION_S - 60)
    harness.task_store.update(_uid("task"), last_run_conversation_id=newer_conv)

    await harness.sweeper.sweep_once()

    assert harness.deleted == [old_conv]
    task = harness.task_store.get(_uid("task"))
    assert task is not None
    assert task.last_run_conversation_id == newer_conv


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
    assert parse_scheduled_tasks_config(raw).session_retention_seconds == expected


def test_parse_sweep_interval() -> None:
    assert parse_scheduled_tasks_config({}).retention_sweep_interval_seconds == 300
    config = parse_scheduled_tasks_config({"retention_sweep_interval_seconds": 2})
    assert config.retention_sweep_interval_seconds == 2


@pytest.mark.parametrize(
    "raw",
    [
        "3600",
        [],
        {"session_retention_seconds": -1},
        {"session_retention_seconds": "1h"},
        {"session_retention_seconds": True},
        {"retention_sweep_interval_seconds": 0},
        {"retention_sweep_interval_seconds": "5"},
    ],
)
def test_parse_scheduled_tasks_config_rejects_malformed(raw: object) -> None:
    with pytest.raises(ValueError, match="scheduled_tasks"):
        parse_scheduled_tasks_config(raw)
