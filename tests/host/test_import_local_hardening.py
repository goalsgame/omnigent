"""Host side of local-session import: heartbeats, cancel, failure codes, missing SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omnigent.host import connect as host_connect
from omnigent.host.frames import (
    HostImportLocalByIdFrame,
    HostImportLocalCancelFrame,
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
    HostImportLocalProgressFrame,
    HostImportLocalSessionChunkFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.session_import.errors import (
    MISSING_SQLITE_FIX_COMMANDS,
    MISSING_SQLITE_MESSAGE,
    ImportErrorCode,
    mentions_missing_sqlite,
)
from tests.server.import_tunnel_harness import (
    RecordingWs,
    local_session,
    make_host,
    serve_local_sessions,
    wait_until,
)

_MISSING_SQLITE = "No module named '_sqlite3'"


def _done(ws: RecordingWs) -> HostImportLocalDoneFrame:
    (done,) = [f for f in ws.frames() if isinstance(f, HostImportLocalDoneFrame)]
    return done


def _host_events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        dict(getattr(record, "attributes", {}))
        for record in caplog.records
        if record.name == host_connect.__name__ and getattr(record, "event_name", None) == name
    ]


def test_request_frames_round_trip_the_progress_flag() -> None:
    """Both import request frames carry the server's heartbeat capability."""
    recent = HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    assert decode_host_frame(encode_host_frame(recent)) == recent
    by_id = HostImportLocalByIdFrame(request_id="r", source="codex", session_id="s", progress=True)
    assert decode_host_frame(encode_host_frame(by_id)) == by_id


def test_request_without_progress_flag_decodes_as_unsupported() -> None:
    """A request from a server that predates heartbeats decodes with progress off."""
    legacy = decode_host_frame(
        json.dumps({"kind": "host.import_local", "request_id": "r", "source": "all", "limit": 5})
    )
    assert legacy == HostImportLocalFrame(request_id="r", source="all", limit=5, progress=False)


def test_progress_and_cancel_frames_round_trip() -> None:
    """Heartbeat (with and without a total) and cancel frames survive encode/decode."""
    for frame in (
        HostImportLocalProgressFrame(request_id="r", done=2, total=None),
        HostImportLocalProgressFrame(request_id="r", done=2, total=7),
        HostImportLocalCancelFrame(request_id="r"),
    ):
        assert decode_host_frame(encode_host_frame(frame)) == frame


async def test_host_omits_heartbeats_for_a_server_that_did_not_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the progress flag the host sends only session and done frames."""
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    assert [json.loads(text)["kind"] for text in ws.sent] == [
        "host.import_local_session",
        "host.import_local_done",
    ]


async def test_heartbeats_count_sessions_done_of_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeats start before enumeration (no total) and advance before each session."""
    sessions: dict[str, Any] = {"s0": local_session("s0"), "bad": OSError("disk")}
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    )
    beats = [(f.done, f.total) for f in ws.frames() if isinstance(f, HostImportLocalProgressFrame)]
    # A failed session advances the count too.
    assert beats == [(0, None), (0, 2), (1, 2)]


async def test_heartbeats_cover_a_slow_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript read slower than the heartbeat interval keeps sending heartbeats."""
    monkeypatch.setattr(host_connect, "_IMPORT_PROGRESS_INTERVAL_S", 0.05)
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=0.4)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    )
    beats = [f for f in ws.frames() if isinstance(f, HostImportLocalProgressFrame)]
    # Start, before the session, and several during the 0.4 s read.
    assert len(beats) >= 4
    # Heartbeats stop with the import.
    sent = len(ws.sent)
    await asyncio.sleep(0.15)
    assert len(ws.sent) == sent


async def test_cancel_frame_stops_the_named_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cancel frame stops its in-flight import, which records itself as cancelled."""
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=1.0)
    host = make_host()
    ws = RecordingWs()
    request = HostImportLocalFrame(request_id="req-2", source="all", limit=5)
    with caplog.at_level(logging.INFO, logger=host_connect.__name__):
        task = asyncio.create_task(host._handle_import_local(ws.as_ws(), request))
        await wait_until(lambda: "req-2" in host._import_tasks)
        await host._handle_raw_message(
            ws.as_ws(), encode_host_frame(HostImportLocalCancelFrame(request_id="req-2"))
        )
        with pytest.raises(asyncio.CancelledError):
            await task
    assert host._import_tasks == {}
    # Cancelled before the done frame: nothing terminal is sent.
    assert not [f for f in ws.frames() if isinstance(f, HostImportLocalDoneFrame)]
    (finished,) = _host_events(caplog, "import_local_finished")
    assert finished["status"] == "cancelled"


async def test_unknown_frame_and_unmatched_cancel_are_ignored() -> None:
    """An unknown kind and a cancel for no in-flight import are dropped silently."""
    host = make_host()
    ws = RecordingWs()
    await host._handle_raw_message(
        ws.as_ws(), json.dumps({"kind": "host.import_local_future", "request_id": "r"})
    )
    await host._handle_raw_message(
        ws.as_ws(), encode_host_frame(HostImportLocalCancelFrame(request_id="nope"))
    )
    assert ws.sent == []


async def test_unexpected_session_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected per-session error reports a generic reason without its text or a code."""
    sessions: dict[str, Any] = {"bad": RuntimeError("/secret/path"), "ok": local_session("ok")}
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    assert _done(ws).failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": "This session could not be read.",
        }
    ]


async def test_missing_sqlite_session_failure_carries_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that fails on a missing SQLite module is reported with the fix and its code."""
    sessions: dict[str, Any] = {
        "bad": ModuleNotFoundError(_MISSING_SQLITE),
        "ok": local_session("ok"),
    }
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    done = _done(ws)
    assert done.status == "ok"
    assert done.failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": MISSING_SQLITE_MESSAGE,
            "code": ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
        }
    ]


async def test_single_harness_listing_error_fails_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any error listing one requested harness becomes the import's failed done frame."""

    def _broken(_source: str, *, limit: int) -> list[str]:
        raise ModuleNotFoundError(_MISSING_SQLITE)

    monkeypatch.setattr("omnigent.session_import.local.list_recent_local_session_ids", _broken)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="codex", limit=5)
    )
    done = _done(ws)
    assert done.status == "failed"
    assert mentions_missing_sqlite(done.error)


async def test_host_logs_start_and_finish_with_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The host records each import's start and its sent, chunked, and failed counts."""
    monkeypatch.setattr("omnigent.host.frames.IMPORT_SESSION_CHUNK_CHARS", 256)
    sessions: dict[str, Any] = {
        "ok": local_session("ok"),
        "unreadable": OSError("disk"),
        "huge": local_session("huge", items=20, text="z" * 50),
    }
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    request = HostImportLocalFrame(
        request_id="req-1", source="all", limit=5, progress=True, allow_session_chunks=True
    )
    with caplog.at_level(logging.INFO, logger=host_connect.__name__):
        await make_host()._handle_import_local(ws.as_ws(), request)
    (started,) = _host_events(caplog, "import_local_started")
    assert (started["request_id"], started["source"], started["progress"]) == (
        "req-1",
        "all",
        True,
    )
    assert started["allow_session_chunks"] is True
    (finished,) = _host_events(caplog, "import_local_finished")
    assert finished["status"] == "ok"
    assert (finished["total"], finished["sent"], finished["chunked"], finished["failed"]) == (
        3,
        2,
        1,
        1,
    )
    assert isinstance(finished["duration_ms"], int)
    assert any(isinstance(f, HostImportLocalSessionChunkFrame) for f in ws.frames())
    assert any(isinstance(f, HostImportLocalSessionFrame) for f in ws.frames())


def test_missing_sqlite_message_is_actionable_and_short() -> None:
    """The missing-SQLite message names the module and comes with per-OS fix commands."""
    assert len(MISSING_SQLITE_MESSAGE) < 450
    assert "_sqlite3" in MISSING_SQLITE_MESSAGE
    assert "omnigent host" in MISSING_SQLITE_MESSAGE
    assert [cmd.split(":")[0] for cmd in MISSING_SQLITE_FIX_COMMANDS] == ["macOS", "Linux"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ModuleNotFoundError: No module named '_sqlite3'", True),
        ("No module named 'sqlite3'", True),
        ("No module named 'yaml'", False),
        (None, False),
    ],
)
def test_mentions_missing_sqlite_detects_both_spellings(text: object, expected: bool) -> None:
    """Both spellings of the missing-SQLite import error are recognized."""
    assert mentions_missing_sqlite(text) is expected


def test_host_import_modules_load_without_sqlite(tmp_path: Path) -> None:
    """The host daemon and transcript readers import on a Python built without SQLite."""
    Path(tmp_path, "state_5.sqlite").write_text("not a db")
    # A fresh interpreter, because this one already has sqlite3 loaded.
    script = textwrap.dedent(
        f"""
        import sys
        sys.modules["_sqlite3"] = None
        sys.modules["sqlite3"] = None
        from pathlib import Path
        import omnigent.host.connect
        from omnigent.session_import import local
        assert local._codex_native_title(Path({str(tmp_path)!r}), "thread-1") is None
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert result.stdout.strip().endswith("ok")
