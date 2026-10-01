"""Continuing an interrupted batch import: the re-run skips what the server already has."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.host.frames import (
    MAX_IMPORT_SKIP_IDS,
    HostImportLocalFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import ImportErrorCode
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    error_event,
    host_record,
    imports_app,
    local_session,
    post_stream,
    register_host,
    serve_local_sessions,
)


def _requests(pair: TunnelPair) -> list[HostImportLocalFrame]:
    """Every batch import request the server sent the host."""
    return [f for f in pair.to_host if isinstance(f, HostImportLocalFrame)]


def _sent_session_ids(pair: TunnelPair, since: int) -> list[str]:
    """External ids of the session frames the host sent after frame ``since``."""
    frames = [decode_host_frame(text) for text in pair.host_ws.sent[since:]]
    return [
        f.session.external_session_id for f in frames if isinstance(f, HostImportLocalSessionFrame)
    ]


async def test_rerun_after_the_time_limit_skips_what_the_server_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run after the time limit skips stored sessions; a completed run forgets them."""
    store = FakeConversationStore()
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(6)}
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, sessions, load_delay_s=0.1)
    async with pair:
        monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 0.35)
        first = await post_stream(app)
        assert error_event(first)["code"] == ImportErrorCode.TIME_LIMIT_REACHED
        have = set(store.external)
        assert 0 < len(have) < 6
        await asyncio.sleep(0.2)  # the cancelled host task winds down

        monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 270.0)
        sent_before = len(pair.host_ws.sent)
        second = await post_stream(app)
        assert set(_requests(pair)[-1].skip_external_session_ids) == have
        # The host skipped them unread; only the rest crossed the tunnel.
        assert set(_sent_session_ids(pair, sent_before)) == set(sessions) - have
        done = second[-1]
        assert (done["imported"], done["already_imported"], done["failed"]) == (
            6 - len(have),
            len(have),
            0,
        )
        assert done["complete"] is True

        # A completed run leaves nothing to continue.
        third = await post_stream(app)
        assert _requests(pair)[-1].skip_external_session_ids == []
        assert third[-1]["already_imported"] == 6


async def test_host_without_the_capability_gets_no_skip_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that doesn't advertise skip support is sent no skip list and reads everything."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    pair = TunnelPair(legacy_host=True)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app)
    assert _requests(pair)[-1].skip_external_session_ids == []
    assert events[-1]["imported"] == 1


async def test_exact_session_import_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing one exact session reads it even when it is on the skip list."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app, source="claude", session_id="s0")
    assert events[-1]["imported"] == 1
    # The exact import leaves the batch's skip list for its own re-run.
    assert imports_module._continue_skip_ids(None, HOST_ID) == ["s0"]


async def test_host_reported_skips_count_as_already_imported() -> None:
    """Skips from heartbeats and the done frame are folded into already_imported once each."""
    registry = HostRegistry()
    conn = register_host(registry)
    app = imports_app(FakeConversationStore(), host_registry=registry, host=host_record())

    async def scripted_host() -> None:
        await conn.outbound_queue.get()
        (queue,) = conn.pending_import_local.values()
        for event in (
            ("progress", {"done": 0, "total": 3, "skipped": 0}),
            ("progress", {"done": 1, "total": 3, "skipped": 1}),
            ("progress", {"done": 2, "total": 3, "skipped": 2}),
            ("done", {"status": "ok", "skipped": 3}),
        ):
            queue.put_nowait(event)

    host = asyncio.create_task(scripted_host())
    events = await post_stream(app)
    await host
    done = events[-1]
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 3, 0)
    assert done["total"] == 3


def test_remembered_ids_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remembered skip ids are dropped once their TTL passes."""
    monkeypatch.setattr(imports_module, "_CONTINUE_SKIP_TTL_S", 0)
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    assert imports_module._continue_skip_ids(None, HOST_ID) == []


def test_remembered_ids_are_capped_to_the_newest() -> None:
    """At most the cap is remembered, keeping the most recently confirmed ids."""
    ids = [f"s{i}" for i in range(MAX_IMPORT_SKIP_IDS + 50)]
    imports_module._remember_continue_skip_ids(None, HOST_ID, ids)
    kept = imports_module._continue_skip_ids(None, HOST_ID)
    assert len(kept) == MAX_IMPORT_SKIP_IDS
    assert ids[-1] in kept
    assert ids[0] not in kept


def test_remembered_ids_are_per_user_and_host() -> None:
    """One user's or host's skip list never applies to another."""
    imports_module._remember_continue_skip_ids("alice", HOST_ID, ["s0"])
    assert imports_module._continue_skip_ids("bob", HOST_ID) == []
    assert imports_module._continue_skip_ids("alice", "other-host") == []
    assert imports_module._continue_skip_ids("alice", HOST_ID) == ["s0"]


def test_nothing_is_remembered_for_an_empty_run() -> None:
    """An interrupted run that confirmed nothing leaves no entry."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, [])
    assert imports_module._CONTINUE_SKIP_IDS.get((None, HOST_ID)) is None
