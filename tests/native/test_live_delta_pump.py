"""Tests for :mod:`omnigent.native._live_delta_pump`."""

from __future__ import annotations

import asyncio

from omnigent.native._live_delta_pump import LiveDelta, LiveDeltaPump


class _GatedPost:
    """Record posted deltas; every post waits for ``gate``."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.posted: list[LiveDelta[str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, delta: LiveDelta[str]) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        try:
            await self.gate.wait()
            self.posted.append(delta)
        finally:
            self.in_flight -= 1


async def test_merges_same_stream_behind_in_flight_post() -> None:
    post = _GatedPost()
    pump: LiveDeltaPump[str] = LiveDeltaPump(post, name="test-pump")
    pump.push("reasoning", "a", "one", started=True)
    await asyncio.wait_for(post.started.wait(), timeout=1.0)
    pump.push("reasoning", "a", " two")
    pump.push("reasoning", "a", " three")
    pump.push("text", "a", "x")
    pump.push("text", "a", "y")
    pump.push("reasoning", "b", "new", started=True)
    pump.push("reasoning", "b", "er")
    post.gate.set()
    await asyncio.wait_for(pump.flush(), timeout=1.0)

    assert [(d.kind, d.key, d.delta, d.started) for d in post.posted] == [
        ("reasoning", "a", "one", True),
        ("reasoning", "a", " two three", False),
        ("text", "a", "xy", False),
        ("reasoning", "b", "newer", True),
    ]
    assert post.max_in_flight == 1


async def test_started_delta_is_not_merged_into_pending_one() -> None:
    post = _GatedPost()
    pump: LiveDeltaPump[str] = LiveDeltaPump(post, name="test-pump")
    pump.push("reasoning", "a", "first", started=True)
    await asyncio.wait_for(post.started.wait(), timeout=1.0)
    pump.push("reasoning", "a", "tail")
    pump.push("reasoning", "a", "again", started=True)
    post.gate.set()
    await asyncio.wait_for(pump.flush(), timeout=1.0)

    assert [(d.delta, d.started) for d in post.posted] == [
        ("first", True),
        ("tail", False),
        ("again", True),
    ]


async def test_cancelled_flush_leaves_post_running() -> None:
    post = _GatedPost()
    pump: LiveDeltaPump[str] = LiveDeltaPump(post, name="test-pump")
    pump.push("text", "a", "hello")
    flusher = asyncio.create_task(pump.flush())
    await asyncio.wait_for(post.started.wait(), timeout=1.0)
    flusher.cancel()
    await asyncio.gather(flusher, return_exceptions=True)
    post.gate.set()
    await asyncio.wait_for(pump.flush(), timeout=1.0)

    assert [d.delta for d in post.posted] == ["hello"]


async def test_aclose_cancels_in_flight_post_and_drops_pending() -> None:
    post = _GatedPost()
    pump: LiveDeltaPump[str] = LiveDeltaPump(post, name="test-pump")
    pump.push("text", "a", "hello")
    await asyncio.wait_for(post.started.wait(), timeout=1.0)
    pump.push("text", "a", " world")
    await asyncio.wait_for(pump.aclose(), timeout=1.0)

    assert post.posted == []
    assert post.in_flight == 0
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "test-pump"]
    await asyncio.wait_for(pump.flush(), timeout=1.0)


async def test_failed_post_is_dropped_and_pump_continues() -> None:
    posted: list[str] = []

    async def _post(delta: LiveDelta[str]) -> None:
        if delta.delta == "boom":
            raise RuntimeError("post failed")
        posted.append(delta.delta)

    pump: LiveDeltaPump[str] = LiveDeltaPump(_post, name="test-pump")
    pump.push("text", "a", "boom")
    pump.push("text", "b", "ok")
    await asyncio.wait_for(pump.flush(), timeout=1.0)

    assert posted == ["ok"]
