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
