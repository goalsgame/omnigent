"""Startup measurements must preserve lifecycle semantics and omit secret inputs."""

import asyncio
import json
import logging
from dataclasses import dataclass

import pytest

from omnigent.startup_timing import startup_span, startup_timed


@pytest.fixture(autouse=True)
def enable_timings(monkeypatch):
    monkeypatch.setenv("OMNIGENT_STARTUP_TIMING", "1")
    monkeypatch.delenv("OMNIGENT_HOST_ID", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", raising=False)


def records(caplog):
    return [
        json.loads(record.getMessage().removeprefix("startup_timing "))
        for record in caplog.records
        if record.name == "omnigent.startup_timing"
    ]


@pytest.mark.asyncio
async def test_thread_work_is_correlated_and_private_inputs_are_omitted(caplog):
    caplog.set_level(logging.INFO, logger="omnigent.startup_timing")

    @startup_timed("worker")
    def worker(token, payload):
        return payload["answer"]

    @startup_timed("launch", session_argument="session_id", host_argument="host_id")
    async def launch(*, session_id, host_id, token):
        return await asyncio.to_thread(worker, token, {"answer": 42, "credential": token})

    assert (
        await launch(session_id="session-test", host_id="host-test", token="private-token") == 42
    )
    events = records(caplog)
    assert [(e["phase"], e["event"]) for e in events] == [
        ("launch", "begin"),
        ("worker", "begin"),
        ("worker", "end"),
        ("launch", "end"),
    ]
    assert all(e["session_id"] == "session-test" and e["host_id"] == "host-test" for e in events)
    assert events[1]["parent_span_id"] == events[0]["span_id"]
    assert events[2]["span_id"] == events[1]["span_id"]
    assert events[3]["duration_ms"] >= events[2]["duration_ms"] >= 0
    assert "private-token" not in caplog.text
    assert "credential" not in caplog.text
    assert "answer" not in caplog.text


def test_exception_is_preserved_without_logging_its_secret_message(caplog):
    caplog.set_level(logging.INFO, logger="omnigent.startup_timing")
    error = RuntimeError("private-token")
    with pytest.raises(RuntimeError) as raised:
        with startup_span("failed", session_id="failed-session"):
            raise error
    assert raised.value is error
    with startup_span("next"):
        pass
    events = records(caplog)
    assert events[1]["outcome"] == "raised"
    assert "session_id" not in events[2]
    assert "parent_span_id" not in events[2]
    assert "private-token" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_initialization_finishes_span_and_does_not_swallow_cancellation(caplog):
    caplog.set_level(logging.INFO, logger="omnigent.startup_timing")
    entered = asyncio.Event()

    @startup_timed("initialize", session_argument="body")
    async def initialize(body):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(initialize({"session_id": "cancelled-session", "token": "secret"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = records(caplog)
    assert len(events) == 2
    assert events[1]["outcome"] == "raised"
    assert events[1]["session_id"] == "cancelled-session"
    assert "secret" not in caplog.text


def test_host_frame_correlation_excludes_other_frame_fields(caplog):
    caplog.set_level(logging.INFO, logger="omnigent.startup_timing")

    @dataclass
    class Frame:
        session_id: str
        token: str

    @startup_timed("host.launch", session_argument="frame")
    def launch(frame):
        return frame.session_id

    assert launch(Frame("frame-session", "private-frame-token")) == "frame-session"
    assert all(event["session_id"] == "frame-session" for event in records(caplog))
    assert "private-frame-token" not in caplog.text
