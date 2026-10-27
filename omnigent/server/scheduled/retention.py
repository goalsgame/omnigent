"""Retention sweep that deletes the sessions scheduled-task runs started.

A run that reached a terminal state keeps its session for the configured
retention window. The sweep reads the persisted finish time of each run, so
retention survives server restarts. A session a person pinned or continued is
kept and its run keeps its ``conversation_id``; the engagement check and the
delete are one transaction in the conversation store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation, ScheduledTaskRun

_logger = logging.getLogger(__name__)

DEFAULT_SESSION_RETENTION_SECONDS = 3600
DEFAULT_SWEEP_INTERVAL_SECONDS = 300
_SWEEP_PAGE_SIZE = 100

# ``delete_session(session_id, conversation, finished_at)`` deletes the session
# unless it is engaged (pinned, or a user message at or after ``finished_at`` in its
# subtree) and returns ``"deleted"``, ``"engaged"`` or ``"missing"``.
DeleteSession = Callable[
    [str, Conversation, int], Awaitable[Literal["deleted", "engaged", "missing"]]
]


@dataclass(frozen=True)
class ScheduledTasksConfig:
    """The server config's ``scheduled_tasks:`` section.

    :param session_retention_seconds: Seconds a finished run's session is kept,
        or ``None`` when retention is disabled.
    :param retention_sweep_interval_seconds: Seconds between retention sweeps.
    """

    session_retention_seconds: int | None = DEFAULT_SESSION_RETENTION_SECONDS
    retention_sweep_interval_seconds: int = DEFAULT_SWEEP_INTERVAL_SECONDS


def parse_scheduled_tasks_config(raw: object) -> ScheduledTasksConfig:
    """Parse the server config's ``scheduled_tasks:`` section.

    ``session_retention_seconds`` defaults to
    :data:`DEFAULT_SESSION_RETENTION_SECONDS`; null or 0 disables retention.
    ``retention_sweep_interval_seconds`` must be a positive integer.

    :param raw: The raw ``scheduled_tasks`` value, or ``None`` when absent.
    :raises ValueError: When the section or a value is malformed.
    """
    if raw is None:
        return ScheduledTasksConfig()
    if not isinstance(raw, dict):
        raise ValueError("server config 'scheduled_tasks' must be a mapping")
    retention: int | None = DEFAULT_SESSION_RETENTION_SECONDS
    if "session_retention_seconds" in raw:
        value = raw["session_retention_seconds"]
        if value is None:
            retention = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "scheduled_tasks.session_retention_seconds must be a non-negative integer or null"
            )
        else:
            retention = value or None
    interval = raw.get("retention_sweep_interval_seconds", DEFAULT_SWEEP_INTERVAL_SECONDS)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError(
            "scheduled_tasks.retention_sweep_interval_seconds must be a positive integer"
        )
    return ScheduledTasksConfig(retention, interval)


class ScheduledSessionRetention:
    """Periodically deletes sessions of scheduled-task runs past the retention window.

    :param store: Scheduled-task store listing expired runs.
    :param conversation_store: Store used to inspect each session.
    :param delete_session: Deletes a session with the same cleanup as the
        delete-session route.
    :param retention_seconds: Seconds a finished run's session is kept.
    :param sweep_interval_seconds: Seconds between sweeps.
    :param clock: Returns the current epoch seconds.
    """

    def __init__(
        self,
        *,
        store: Any,
        conversation_store: Any,
        delete_session: DeleteSession,
        retention_seconds: int,
        sweep_interval_seconds: int = DEFAULT_SWEEP_INTERVAL_SECONDS,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self._store = store
        self._conversation_store = conversation_store
        self._delete_session = delete_session
        self._retention_seconds = retention_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the sweep loop; a no-op if it is already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="scheduled-session-retention")

    async def shutdown(self) -> None:
        """Stop the sweep loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self) -> int:
        """Run one cross-workspace sweep.

        :returns: The number of sessions deleted.
        """
        cutoff = self._clock() - self._retention_seconds
        deleted = 0
        cursor: tuple[int, str] | None = None
        while True:
            page = await asyncio.to_thread(
                self._store.list_expired_session_runs,
                cutoff,
                limit=_SWEEP_PAGE_SIZE,
                after=cursor,
            )
            for workspace_id, run in page:
                try:
                    with workspace_scope(workspace_id):
                        if await self._expire(run):
                            deleted += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "scheduled session retention: failed to delete session %s of run %s",
                        run.conversation_id,
                        run.id,
                    )
            if len(page) < _SWEEP_PAGE_SIZE:
                return deleted
            last = page[-1][1]
            cursor = (_finish_time(last), last.id)

    async def _run(self) -> None:
        while True:
            try:
                deleted = await self.sweep_once()
                if deleted:
                    _logger.info("Scheduled session retention deleted %s session(s)", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Scheduled session retention sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_seconds)

    async def _expire(self, run: ScheduledTaskRun) -> bool:
        """Delete one run's session unless a person engaged with it.

        :returns: ``True`` if a session was deleted.
        """
        session_id = run.conversation_id
        if session_id is None:
            return False
        finished_at = _finish_time(run)
        conv = await asyncio.to_thread(self._conversation_store.get_conversation, session_id)
        outcome: Literal["deleted", "engaged", "missing"] = "missing"
        if conv is not None:
            outcome = await self._delete_session(session_id, conv, finished_at)
            if outcome == "engaged":
                return False
        await asyncio.to_thread(self._store.clear_run_conversation, run.id)
        return outcome == "deleted"


def _finish_time(run: ScheduledTaskRun) -> int:
    """The run's finish time, falling back to ``fired_at`` then ``scheduled_at``."""
    if run.finished_at is not None:
        return run.finished_at
    return run.fired_at if run.fired_at is not None else run.scheduled_at
