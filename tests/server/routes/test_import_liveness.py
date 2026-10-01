"""Host liveness during a local-session import: offline, unreachable, stalls, deadline."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest

from omnigent.errors import ErrorCode
from omnigent.host.frames import (
    HostImportedLocalSession,
    HostImportLocalCancelFrame,
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
    HostImportLocalProgressFrame,
    HostImportLocalSessionChunkFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import _host_launch, host_tunnel
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import ImportErrorCode, LocalImportError
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    client,
    error_event,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    post_stream,
    register_host,
    serve_local_sessions,
)


async def _drain(registry: HostRegistry, conn: HostConnection, **kwargs: Any) -> list[Any]:
    """Run the stream consumer to completion and return what it yielded."""
    return [
        item
        async for item in imports_module._stream_local_sessions_from_host(
            host_registry=registry, host_conn=conn, source="all", limit=5, **kwargs
        )
    ]


async def _push_after_request(conn: HostConnection, *events: tuple[str, dict[str, Any]]) -> None:
    """Act as the tunnel: wait for the request, then queue ``events`` for it."""
    await conn.outbound_queue.get()
    (queue,) = conn.pending_import_local.values()
    for event in events:
        queue.put_nowait(event)


@pytest.mark.parametrize(
    ("age_s", "expected"),
    [(600, "is offline (last seen 10 min ago)"), (20, "isn't connected right now")],
)
async def test_offline_host_409_names_machine_and_last_seen(age_s: int, expected: str) -> None:
    """A host with no tunnel here gets a 409 naming it and when it was last seen."""
    app = imports_app(
        FakeConversationStore(),
        host_registry=HostRegistry(),
        host=host_record(name="studio-mac", age_s=age_s),
    )
    async with client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body())
    assert response.status_code == 409
    error = response.json()["error"]
    # Old clients key on the global code; new ones on import_code.
    assert error["code"] == ErrorCode.CONFLICT
    assert error["import_code"] == ImportErrorCode.HOST_OFFLINE
    assert error["retryable"] is True
    assert error["host_name"] == "studio-mac"
    assert abs(error["last_seen_seconds"] - age_s) <= 2
    assert "studio-mac" in error["message"]
    assert expected in error["message"]


async def test_live_host_on_another_replica_stays_wrong_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live host on another replica of a sharded deployment is wrong_replica, not offline."""
    monkeypatch.setattr(_host_launch, "_deployment_is_sharded", lambda: True)
    app = imports_app(FakeConversationStore(), host_registry=HostRegistry(), host=host_record())
    async with client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.WRONG_REPLICA


async def test_host_disconnect_mid_batch_reports_progress_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel drop ends the stream at once with host_disconnected and the batch's progress."""
    store = FakeConversationStore()
    pair = TunnelPair(host_name="studio-mac")
    app = imports_app(store, host_registry=pair.registry, host=host_record(name="studio-mac"))
    disconnected_at: list[float] = []

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        if not disconnected_at:
            disconnected_at.append(time.monotonic())
            pair.registry.deregister(HOST_ID, workspace_id=0, conn=pair.conn)

    store.on_append = on_append
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(3)}
    serve_local_sessions(monkeypatch, sessions, load_delay_s=0.05)
    async with pair:
        events = await post_stream(app)

    # Ends right away, not after the per-frame timeout.
    assert time.monotonic() - disconnected_at[0] < 1.0
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_DISCONNECTED
    assert error["retryable"] is True
    imported = events[-1]["imported"]
    assert 1 <= imported < 3
    assert [e["event"] for e in events].count("session") == imported
    assert "studio-mac" in error["message"]
    assert f"disconnected after {imported} of 3 sessions" in error["message"]
    assert (error["host_name"], error["processed"], error["imported"], error["total"]) == (
        "studio-mac",
        imported,
        imported,
        3,
    )
    assert events[-1]["complete"] is False


async def test_silent_tunnel_is_unreachable_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered tunnel silent for over 1.5 ping intervals fails fast as host_unreachable."""
    monkeypatch.setattr(imports_module, "PING_INTERVAL_S", 60.0)
    pair = TunnelPair()
    pair.conn.last_frame_at = time.time() - 200
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    events = await post_stream(app)
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_UNREACHABLE
    assert error["retryable"] is True
    assert "isn't responding" in error["message"]
    assert error["silent_seconds"] >= 199
    # No import request was sent to the host.
    assert pair.conn.outbound_queue.empty()


async def test_recently_heard_tunnel_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tunnel heard from within 1.5 ping intervals imports normally."""
    monkeypatch.setattr(imports_module, "PING_INTERVAL_S", 60.0)
    pair = TunnelPair()
    pair.conn.last_frame_at = time.time() - 70
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app)
    assert events[-1]["event"] == "done"
    assert (events[-1]["imported"], events[-1]["failed"], events[-1]["complete"]) == (1, 0, True)


@pytest.mark.parametrize("legacy_host", [False, True], ids=["current", "legacy"])
async def test_progress_events_report_done_of_total(
    monkeypatch: pytest.MonkeyPatch, legacy_host: bool
) -> None:
    """Progress events count up to the batch total, with or without host heartbeats."""
    pair = TunnelPair(legacy_host=legacy_host)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {f"s{i}": local_session(f"s{i}") for i in range(3)})
    async with pair:
        events = await post_stream(app)
    progress = [e for e in events if e["event"] == "progress"]
    assert progress[-1] == {"event": "progress", "done": 3, "total": 3}
    assert [p["done"] for p in progress] == sorted(p["done"] for p in progress)
    assert events[-1]["imported"] == 3
    assert events[-1]["total"] == 3
    heartbeats = [f for f in pair.host_frames() if isinstance(f, HostImportLocalProgressFrame)]
    # Only a host that honors the request's progress flag sends heartbeats.
    assert bool(heartbeats) is (not legacy_host)


async def test_stall_after_heartbeat_uses_the_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host silent after a heartbeat is host_unresponsive after the short timeout."""
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_HEARTBEAT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 30.0)
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(_push_after_request(conn, ("progress", {"done": 0, "total": 5})))
    started = time.monotonic()
    with pytest.raises(LocalImportError) as raised:
        await _drain(registry, conn)
    await host
    assert raised.value.import_code == ImportErrorCode.HOST_UNRESPONSIVE
    assert time.monotonic() - started < 5


async def test_chunk_progress_keeps_the_long_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk slice proves liveness but does not switch to the heartbeat timeout."""
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_HEARTBEAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 0.5)
    registry = HostRegistry()
    conn = register_host(registry)

    async def host() -> None:
        await _push_after_request(conn, ("progress", {}))
        await asyncio.sleep(0.2)  # past the heartbeat timeout, within the long one
        (queue,) = conn.pending_import_local.values()
        queue.put_nowait(("done", {"status": "ok"}))

    task = asyncio.create_task(host())
    assert await _drain(registry, conn) == []
    await task


async def test_legacy_host_stall_keeps_the_original_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without heartbeats that goes quiet fails after the per-frame timeout."""
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 0.3)
    pair = TunnelPair(legacy_host=True)
    app = imports_app(
        FakeConversationStore(), host_registry=pair.registry, host=host_record(name="studio-mac")
    )
    sessions = {"s0": local_session("s0"), "s1": local_session("s1")}
    serve_local_sessions(monkeypatch, sessions, load_delay_s=1.0)
    async with pair:
        events = await post_stream(app)
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_UNRESPONSIVE
    assert "stopped responding" in error["message"]
    assert "studio-mac" in error["message"]


async def test_deadline_stops_host_and_says_how_to_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole-stream deadline cancels the host's read and reports how far the batch got."""
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 0.5)
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(20)}
    serve_local_sessions(monkeypatch, sessions, load_delay_s=0.1)
    async with pair:
        events = await post_stream(app, limit=20)
        await asyncio.sleep(0.2)
        # The cancel frame stopped the host's import task.
        assert not pair.host._import_tasks
    error = error_event(events)
    assert error["code"] == ImportErrorCode.TIME_LIMIT_REACHED
    assert error["retryable"] is True
    imported = events[-1]["imported"]
    assert 0 < imported < 20
    assert f"Imported {imported} of 20 before the time limit" in error["message"]
    assert pair.cancel_frames()


async def test_buffered_route_reports_time_limit_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """The buffered route turns the deadline into a 503 with the partial tally."""
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 0.3)
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(20)}
    serve_local_sessions(monkeypatch, sessions, load_delay_s=0.1)
    async with pair, client(app) as http:
        response = await http.post("/v1/imports/local", json=local_import_body(limit=20))
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert error["import_code"] == ImportErrorCode.TIME_LIMIT_REACHED
    assert error["error_id"].startswith("err_")
    assert error["imported"] >= 1
    assert error["total"] == 20


async def test_closing_the_stream_early_cancels_the_host_import() -> None:
    """A consumer that stops reading sends the host a cancel frame and drops its queue."""
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(
        _push_after_request(
            conn, ("session", {"external_session_id": "s0", "items": [], "total": 9})
        )
    )
    stream = imports_module._stream_local_sessions_from_host(
        host_registry=registry, host_conn=conn, source="all", limit=9
    )
    await anext(stream)
    await cast(Any, stream).aclose()  # what Starlette does when the client goes away
    await host
    sent = conn.outbound_queue.get_nowait()
    assert sent is not None
    assert isinstance(decode_host_frame(sent), HostImportLocalCancelFrame)
    assert conn.pending_import_local == {}


async def test_finished_stream_sends_no_cancel() -> None:
    """A stream that reached the host's done frame leaves the host alone."""
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(_push_after_request(conn, ("done", {"status": "ok"})))
    assert await _drain(registry, conn) == []
    await host
    assert conn.outbound_queue.empty()


async def test_failed_chunked_session_does_not_reset_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunked session cut off by the done frame fails alone and keeps the batch total."""
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())

    async def scripted_host() -> None:
        request = decode_host_frame(await pair.conn.outbound_queue.get() or "")
        assert isinstance(request, HostImportLocalFrame)
        request_id = request.request_id
        session = HostImportedLocalSession(
            external_session_id="s0",
            workspace="/repo",
            items=[
                {
                    "type": "message",
                    "response_id": "r1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
            source="claude",
        )
        frames = [
            HostImportLocalSessionFrame(request_id=request_id, total=2, session=session),
            # The second session's first slice, never followed by its last.
            HostImportLocalSessionChunkFrame(
                request_id=request_id, total=2, seq=0, last=False, data='{"external_sess'
            ),
            HostImportLocalDoneFrame(request_id=request_id, status="ok"),
        ]
        for frame in frames:
            await pair.server_ws.inbound.put(encode_host_frame(frame))

    receive = asyncio.create_task(
        host_tunnel._receive_loop(
            cast(Any, pair.server_ws),
            pair.conn,
            HOST_ID,
            cast(Any, None),
            pair.registry,
            None,
            None,
            None,
        )
    )
    host = asyncio.create_task(scripted_host())
    try:
        events = await post_stream(app)
    finally:
        receive.cancel()
        host.cancel()
    done = events[-1]
    assert (done["imported"], done["failed"], done["total"], done["complete"]) == (1, 1, 2, True)
    assert [p["total"] for p in events if p["event"] == "progress"] == [2, 2]
