"""Lightweight startup spans with no credentials, payloads or heavyweight imports."""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

_P = ParamSpec("_P")
_T = TypeVar("_T")
_logger = logging.getLogger(__name__)
_context: ContextVar[dict[str, str] | None] = ContextVar("startup_timing_context", default=None)


def startup_timing_enabled() -> bool:
    """Enable diagnostic startup records explicitly on each participating process."""
    return os.environ.get("OMNIGENT_STARTUP_TIMING", "").lower() in {"1", "true", "yes"}


@contextmanager
def startup_stderr_logging() -> Iterator[None]:
    """Capture only safe timing records before ordinary process logging is configured."""
    if not startup_timing_enabled():
        yield
        return
    handler = logging.StreamHandler()
    previous_level = _logger.level
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        _logger.removeHandler(handler)
        handler.close()
        _logger.setLevel(previous_level)


@contextmanager
def startup_span(
    phase: str, *, session_id: str | None = None, host_id: str | None = None
) -> Iterator[None]:
    """Log paired wall-clock events and a monotonic duration; nested times overlap."""
    if not startup_timing_enabled():
        yield
        return
    parent = _context.get() or {}
    fields = dict(parent)
    fields.pop("parent_span_id", None)
    if parent.get("span_id"):
        fields["parent_span_id"] = parent["span_id"]
    fields["span_id"] = uuid.uuid4().hex
    for key, value in (
        ("session_id", session_id or os.environ.get("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")),
        ("host_id", host_id or os.environ.get("OMNIGENT_HOST_ID")),
    ):
        if value:
            fields[key] = value
    token = _context.set(fields)
    started = time.perf_counter()
    outcome = "returned"
    try:
        _emit(phase, "begin", fields)
        yield
    except BaseException:
        outcome = "raised"
        raise
    finally:
        try:
            _emit(
                phase,
                "end",
                fields,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                outcome=outcome,
            )
        finally:
            _context.reset(token)


def _emit(phase: str, event: str, fields: dict[str, str], **metrics: str | float) -> None:
    _logger.info(
        "startup_timing %s",
        json.dumps(
            {
                **fields,
                "phase": phase,
                "event": event,
                "time_ns": time.time_ns(),
                "pid": os.getpid(),
                **metrics,
            },
            separators=(",", ":"),
        ),
    )


def startup_timed(
    phase: str, *, session_argument: str | None = None, host_argument: str | None = None
) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Preserve call semantics and extract only explicitly named identity arguments."""

    def decorate(function: Callable[_P, _T]) -> Callable[_P, _T]:
        signature = inspect.signature(function) if session_argument or host_argument else None

        def identities(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, str | None]:
            if signature is None:
                return {}
            arguments = signature.bind_partial(*args, **kwargs).arguments
            session = arguments.get(session_argument) if session_argument else None
            if isinstance(session, Mapping):
                session = session.get("session_id")
            elif session is not None and not isinstance(session, str):
                session = getattr(session, "session_id", None)
            host = arguments.get(host_argument) if host_argument else None
            return {
                "session_id": session if isinstance(session, str) else None,
                "host_id": host if isinstance(host, str) else None,
            }

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapped(*args: _P.args, **kwargs: _P.kwargs) -> Any:
                with startup_span(phase, **identities(args, kwargs)):
                    return await cast(Callable[_P, Awaitable[Any]], function)(*args, **kwargs)

            return cast(Callable[_P, _T], async_wrapped)

        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with startup_span(phase, **identities(args, kwargs)):
                return function(*args, **kwargs)

        return wrapped

    return decorate
