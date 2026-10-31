"""Single-flight delivery of transient live-output deltas for native forwarders.

Native forwarders mirror live assistant/reasoning output into Omnigent as
``POST /v1/sessions/{id}/events`` deltas. Awaiting each POST inline makes live
output advance one chunk per server round trip. :class:`LiveDeltaPump` lets the
caller queue deltas without waiting: at most one delta POST is in flight, and
deltas queued behind it for the same stream merge into one POST.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

_logger = logging.getLogger(__name__)

K = TypeVar("K", bound=Hashable)


@dataclass
class LiveDelta(Generic[K]):
    """
    One queued live delta, possibly merged from several pushes.

    :param kind: Stream kind, e.g. ``"text"`` or ``"reasoning"``.
    :param key: Stream identity within the kind, e.g. a step index or part id.
    :param delta: Text to post, e.g. ``"hello"``.
    :param started: Whether this chunk opens its stream.
    """

    kind: str
    key: K
    delta: str
    started: bool = False


class LiveDeltaPump(Generic[K]):
    """
    Post queued live deltas in order with at most one POST in flight.

    A pushed delta merges into the last queued (not yet posted) delta when both
    share ``kind`` and ``key`` and the new one does not open a stream. Callers
    that post anything which must follow the queued deltas call :meth:`flush`
    first.

    :param post: Delivers one (possibly merged) delta. Exceptions are logged
        and the delta is dropped.
    :param name: Name of the background task, e.g. ``"opencode-live-deltas"``.
    """

    def __init__(self, post: Callable[[LiveDelta[K]], Awaitable[object]], *, name: str) -> None:
        """
        Initialize an idle pump.

        :param post: Delivers one (possibly merged) delta.
        :param name: Name of the background task.
        """
        self._post = post
        self._name = name
        self._pending: deque[LiveDelta[K]] = deque()
        self._task: asyncio.Task[None] | None = None

    def push(self, kind: str, key: K, delta: str, *, started: bool = False) -> None:
        """
        Queue one delta for delivery without waiting for it to be posted.

        :param kind: Stream kind, e.g. ``"reasoning"``.
        :param key: Stream identity within the kind, e.g. ``"prt_1"``.
        :param delta: Text to post; empty text is ignored.
        :param started: Whether this chunk opens its stream.
        :returns: None.
        """
        if not delta:
            return
        last = self._pending[-1] if self._pending else None
        if last is not None and last.kind == kind and last.key == key and not started:
            last.delta += delta
        else:
            self._pending.append(LiveDelta(kind=kind, key=key, delta=delta, started=started))
        if self._task is None:
            self._task = asyncio.create_task(self._pump(), name=self._name)

    async def flush(self) -> None:
        """
        Wait until every delta queued before and during this call is posted.

        Cancelling the caller does not cancel the in-flight POST.

        :returns: None.
        """
        while (task := self._task) is not None:
            await asyncio.wait({task})

    async def aclose(self) -> None:
        """
        Drop queued deltas and cancel the in-flight POST, if any.

        :returns: None after the background task has finished.
        """
        self._pending.clear()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _pump(self) -> None:
        """Post queued deltas until the queue is empty."""
        try:
            while self._pending:
                item = self._pending.popleft()
                try:
                    await self._post(item)
                except Exception:  # noqa: BLE001 - a failed live delta must not stop the pump.
                    _logger.warning("live %s delta post failed", item.kind, exc_info=True)
        finally:
            if self._task is asyncio.current_task():
                self._task = None
