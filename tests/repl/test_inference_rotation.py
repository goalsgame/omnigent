"""REPL session rotation preserves session-bound inference isolation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from omnigent_client._sessions import Session

from omnigent.inference_config import INFERENCE_HOST_POLICY_LABEL
from omnigent.repl._repl import _SessionsChatReplAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("opted_in", [False, True])
async def test_rotation_requests_fresh_host_runner_only_for_opted_in_sessions(bundled, opted_in):
    old = Session(
        id="old",
        agent_id="agent",
        agent_name="agent",
        status="idle",
        created_at=0,
        runner_id="old-runner",
        host_id="host",
        workspace="/host/workspace",
        labels={INFERENCE_HOST_POLICY_LABEL: "host"} if opted_in else {},
    )
    fresh = Session(
        id="new",
        agent_id="agent",
        agent_name="agent",
        status="idle",
        created_at=1,
        runner_id="new-runner" if opted_in else None,
        host_id="host" if opted_in else None,
        workspace="/host/workspace",
    )
    client = SimpleNamespace(sessions=SimpleNamespace())
    client.sessions.get = AsyncMock(return_value=old)
    client.sessions.post_event = AsyncMock()
    client.sessions.unbind_runner = AsyncMock()
    client.sessions.create = AsyncMock(return_value=fresh)
    client.sessions.create_from_agent_id = AsyncMock(return_value=fresh)
    client.sessions.resolve_online_runner = AsyncMock()
    adapter = _SessionsChatReplAdapter(
        client,
        agent_name="agent",
        session_id="old",
        runner_id="old-runner",
        session_bundle=b"bundle" if bundled else None,
    )
    adapter._hydrate_from_session_snapshot(old)
    adapter._rotation_placement = ("stale-host", "/stale-workspace")
    await adapter.start_new_conversation()
    client.sessions.unbind_runner.assert_awaited_once_with("old")
    if opted_in:
        client.sessions.post_event.assert_awaited_once_with(
            "old", {"type": "stop_session", "data": {}}
        )
        assert adapter._runner_id is None
    else:
        client.sessions.post_event.assert_not_awaited()
        assert adapter._runner_id == "old-runner"
    if bundled:
        await adapter._create_session_from_bundle(pending_debug=False)
        kwargs = client.sessions.create.await_args.kwargs
    else:
        await adapter._create_session_from_registered_agent(pending_debug=False)
        kwargs = client.sessions.create_from_agent_id.await_args.kwargs
    if opted_in:
        assert kwargs["host_id"] == "host"
        assert kwargs["workspace"] == "/host/workspace"
        assert adapter._runner_id == "new-runner"
    else:
        assert "host_id" not in kwargs
        assert adapter._runner_id == "old-runner"
    client.sessions.resolve_online_runner.assert_not_awaited()
    assert adapter._rotation_placement is None


@pytest.mark.asyncio
async def test_failed_rotation_stop_preserves_current_session():
    client = SimpleNamespace(sessions=SimpleNamespace())
    client.sessions.get = AsyncMock(
        return_value=Session(
            id="old",
            agent_id="agent",
            status="idle",
            created_at=0,
            runner_id="runner",
            host_id="host",
            workspace="/work",
            labels={INFERENCE_HOST_POLICY_LABEL: "host"},
        )
    )
    client.sessions.post_event = AsyncMock(side_effect=RuntimeError("stop rejected"))
    client.sessions.unbind_runner = AsyncMock()
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="old", runner_id="runner")
    with pytest.raises(RuntimeError, match="stop rejected"):
        await adapter.start_new_conversation()
    assert adapter.session_id == "old" and adapter._runner_id == "runner"
    assert adapter._rotation_placement is None
    client.sessions.unbind_runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_has_host", [False, True])
async def test_fork_or_unbound_host_session_gets_own_runner(monkeypatch, target_has_host):
    source = Session(
        id="source",
        agent_id="agent",
        status="idle",
        created_at=0,
        runner_id="source-runner",
        host_id="source-host",
        workspace="/source",
        labels={INFERENCE_HOST_POLICY_LABEL: "source-host"},
    )
    host_id = "target-host" if target_has_host else "source-host"
    workspace = "/target" if target_has_host else "/source"
    target = Session(
        id="fork",
        agent_id="agent",
        status="idle",
        created_at=1,
        host_id=host_id if target_has_host else None,
        workspace=workspace if target_has_host else None,
    )
    bound = Session(
        id="fork",
        agent_id="agent",
        status="idle",
        created_at=1,
        runner_id="fork-runner",
        host_id=host_id,
        workspace=workspace,
        labels={INFERENCE_HOST_POLICY_LABEL: host_id},
    )
    client = SimpleNamespace(
        _base_url="https://server.example",
        sessions=SimpleNamespace(
            get=AsyncMock(return_value=target),
            launch_runner=AsyncMock(return_value=bound),
            bind_runner=AsyncMock(),
        ),
    )
    adapter = _SessionsChatReplAdapter(
        client, "agent", session_id="source", runner_id="source-runner"
    )
    adapter._hydrate_from_session_snapshot(source)
    adapter._stream_pump = AsyncMock()
    monkeypatch.setattr(
        "omnigent.cli_auth.databricks_request_headers",
        lambda base, *, host_id: {"X-Test-Host": host_id},
    )
    adapter.switch_session("fork")
    try:
        assert await adapter._ensure_session() == "fork"
        client.sessions.launch_runner.assert_awaited_once_with(
            "fork",
            host_id=host_id,
            workspace=workspace,
            headers={"X-Test-Host": host_id},
        )
        assert adapter._runner_id == "fork-runner"
        client.sessions.bind_runner.assert_not_awaited()
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_only", [False, True])
async def test_switch_from_observed_session_does_not_stop_or_unbind_it(attach_only):
    target = Session(
        id="target",
        agent_id="agent",
        status="idle",
        created_at=0,
        runner_id="target-runner",
    )
    client = SimpleNamespace(
        sessions=SimpleNamespace(
            get=AsyncMock(return_value=target),
            post_event=AsyncMock(),
            unbind_runner=AsyncMock(),
            bind_runner=AsyncMock(),
        )
    )
    adapter = _SessionsChatReplAdapter(
        client,
        "agent",
        session_id="observed",
        runner_id="observed-runner",
        attach_only=attach_only,
    )
    adapter._readonly_view = not attach_only
    adapter._stream_pump = AsyncMock()
    try:
        await adapter.switch_to_session("target")
        assert adapter.session_id == "target"
        client.sessions.post_event.assert_not_awaited()
        client.sessions.unbind_runner.assert_not_awaited()
        client.sessions.bind_runner.assert_not_awaited()
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_new_from_observed_proxy_session_uses_own_runner_without_detaching_source():
    source = Session(
        id="observed",
        agent_id="agent",
        status="idle",
        created_at=0,
        runner_id="observed-runner",
        host_id="host",
        workspace="/work",
        labels={INFERENCE_HOST_POLICY_LABEL: "host"},
    )
    fresh = Session(id="fresh", agent_id="agent", status="idle", created_at=1)
    bound = Session(
        id="fresh",
        agent_id="agent",
        status="idle",
        created_at=1,
        runner_id="fresh-runner",
        host_id="host",
        workspace="/work",
        labels={INFERENCE_HOST_POLICY_LABEL: "host"},
    )
    client = SimpleNamespace(
        _base_url="https://server.example",
        sessions=SimpleNamespace(
            create=AsyncMock(return_value=fresh),
            launch_runner=AsyncMock(return_value=bound),
            post_event=AsyncMock(),
            unbind_runner=AsyncMock(),
            bind_runner=AsyncMock(),
        ),
    )
    adapter = _SessionsChatReplAdapter(
        client, "agent", session_id="observed", session_bundle=b"bundle"
    )
    adapter._hydrate_from_session_snapshot(source)
    adapter._readonly_view = True
    adapter._stream_pump = AsyncMock()
    try:
        await adapter.start_new_conversation()
        assert await adapter._ensure_session() == "fresh"
        assert adapter._runner_id == "fresh-runner"
        client.sessions.post_event.assert_not_awaited()
        client.sessions.unbind_runner.assert_not_awaited()
        client.sessions.bind_runner.assert_not_awaited()
        client.sessions.launch_runner.assert_awaited_once()
    finally:
        await adapter.aclose()
