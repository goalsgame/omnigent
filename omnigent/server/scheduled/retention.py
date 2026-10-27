"""Retention sweep that deletes the sessions scheduled-task runs started.

A run that reached a terminal state keeps its session for the configured
retention window. The sweep reads the persisted ``finished_at`` of each run, so
retention survives server restarts. A session a person pinned or continued is
kept and its run keeps its ``conversation_id``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation, ScheduledTaskRun
from omnigent.stores.conversation_store import PINNED_LABEL_KEY

_logger = logging.getLogger(__name__)

DEFAULT_SESSION_RETENTION_SECONDS = 3600
DEFAULT_SWEEP_INTERVAL_SECONDS = 300
_SWEEP_PAGE_SIZE = 100
_RECENT_MESSAGE_SCAN_LIMIT = 50

# ``delete_session(session_id, conversation)`` returns False if the row was already gone.
DeleteSession = Callable[[str, Conversation], Awaitable[bool]]


def parse_session_retention_seconds(raw: object) -> int | None:
    """Parse the server config's ``scheduled_tasks:`` section.

    :param raw: The raw ``scheduled_tasks`` value, or ``None`` when absent.
    :returns: Retention in seconds, or ``None`` when retention is disabled
        (``session_retention_seconds`` is null or 0). Defaults to
        :data:`DEFAULT_SESSION_RETENTION_SECONDS` when the key is absent.
    :raises ValueError: When the section or the value is malformed.
    """
    if raw is None:
        return DEFAULT_SESSION_RETENTION_SECONDS
    if not isinstance(raw, dict):
        raise ValueError("server config 'scheduled_tasks' must be a mapping")
    if "session_retention_seconds" not in raw:
        return DEFAULT_SESSION_RETENTION_SECONDS
    value = raw["session_retention_seconds"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            "scheduled_tasks.session_retention_seconds must be a non-negative integer or null"
        )
    return value or None


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
            if last.finished_at is None:
                return deleted
            cursor = (last.finished_at, last.id)

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
        if session_id is None or run.finished_at is None:
            return False
        conv = await asyncio.to_thread(self._conversation_store.get_conversation, session_id)
        deleted = False
        if conv is not None:
            if await self._is_engaged(conv, run.finished_at):
                return False
            deleted = await self._delete_session(session_id, conv)
        await asyncio.to_thread(self._store.clear_run_conversation, run.id)
        await asyncio.to_thread(self._clear_task_pointer, run.scheduled_task_id, session_id)
        return deleted

    def _clear_task_pointer(self, task_id: str, session_id: str) -> None:
        task = self._store.get(task_id)
        if task is not None and task.last_run_conversation_id == session_id:
            self._store.update(task_id, last_run_conversation_id=None)

    async def _is_engaged(self, conv: Conversation, finished_at: int) -> bool:
        """Whether the session is pinned or has a user message after the run finished."""
        if any(
            key == PINNED_LABEL_KEY or key.startswith(f"{PINNED_LABEL_KEY}.")
            for key in conv.labels
        ):
            return True
        page = await asyncio.to_thread(
            self._conversation_store.list_items,
            conv.id,
            limit=_RECENT_MESSAGE_SCAN_LIMIT,
            order="desc",
            type="message",
        )
        return any(
            getattr(item.data, "role", None) == "user" and item.created_at > finished_at
            for item in page.data
        )
