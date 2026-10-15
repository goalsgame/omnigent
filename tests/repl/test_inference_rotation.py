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
async def test_rotation_requests_fresh_host_runner_only_for_opted_in_sessions(
    bundled, opted_in, monkeypatch
):
    from dataclasses import replace

    from omnigent import chat

    monkeypatch.setattr(chat, "_session_hosts", {})
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
    adapter._hydrate_from_session_snapshot(
        replace(
            old,
            host_id="previous-host",
            workspace="/previous",
            labels={INFERENCE_HOST_POLICY_LABEL: "previous-host"} if opted_in else {},
        )
    )

    async def stop_on_current_host(*args, **kwargs):
        assert chat.get_session_host("old") == "host"

    client.sessions.post_event = AsyncMock(side_effect=stop_on_current_host)
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


@pytest.mark.asyncio
async def test_cross_host_launch_repoints_real_auth_and_ignores_stale_snapshot(monkeypatch):
    import httpx
    from omnigent_client import OmnigentClient

    from omnigent import chat
    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER

    base = "https://workspace.example/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "synthetic-token")
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setattr(chat, "_session_hosts", {})
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda url: None)
    monkeypatch.setattr("omnigent.telemetry.request_headers.telemetry_request_headers", dict)
    source = Session(
        id="source",
        agent_id="agent",
        status="idle",
        created_at=0,
        runner_id="runner-a",
        host_id="host-a",
        workspace="/a",
        labels={INFERENCE_HOST_POLICY_LABEL: "host-a"},
    )
    target_body = {
        "id": "target",
        "agent_id": "agent",
        "status": "idle",
        "created_at": 0,
        "host_id": "host-b",
        "workspace": "/b",
        "runner_id": None,
        "labels": {INFERENCE_HOST_POLICY_LABEL: "host-b"},
    }
    launched = False
    requests = []

    def handler(request):
        nonlocal launched
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.method == "POST" and request.url.path.endswith("/hosts/host-b/runners"):
            assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host-b"
            launched = True
            return httpx.Response(200, json={"runner_id": "runner-b"})
        if launched:
            assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host-b"
        if request.method == "POST":
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(
            200, json={**target_body, "runner_id": "runner-b" if launched else None}
        )

    auth = chat._DatabricksTokenAuth(base, session_id="source")
    async with OmnigentClient(base_url=base, auth=auth) as client:
        await client._http._transport.aclose()
        client._http._transport = httpx.MockTransport(handler)
        client._http._mounts.clear()
        adapter = _SessionsChatReplAdapter(
            client, "agent", session_id="source", runner_id="runner-a"
        )
        adapter._hydrate_from_session_snapshot(source)
        adapter._stream_pump = AsyncMock()
        adapter.switch_session("target")
        try:
            await adapter._ensure_session()
            assert launched and adapter._runner_id == "runner-b"
            await client.sessions.post_event("target", {"type": "message", "data": {}})
            # A late source refresh cannot overwrite the target's routing or binding.
            adapter._hydrate_from_session_snapshot(source)
            assert adapter._runner_id == "runner-b"
            await client.sessions.post_event("target", {"type": "message", "data": {}})
        finally:
            await adapter.aclose()
    assert requests[0].headers[OMNIGENT_SLICE_KEY_HEADER] == "host-a"
    assert all(request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host-b" for request in requests[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "stop"])
async def test_transition_does_not_stop_a_new_selection_or_discard_it(phase):
    old = Session(
        id="old",
        agent_id="agent",
        status="idle",
        created_at=0,
        runner_id="runner",
        host_id="host",
        workspace="/work",
        labels={INFERENCE_HOST_POLICY_LABEL: "host"},
    )
    client = SimpleNamespace(
        sessions=SimpleNamespace(
            get=AsyncMock(),
            post_event=AsyncMock(),
            unbind_runner=AsyncMock(),
        )
    )
    adapter = _SessionsChatReplAdapter(client, "agent", session_id="old", runner_id="runner")

    async def change_session(*args):
        adapter.switch_session("other")
        return old

    if phase == "read":
        client.sessions.get.side_effect = change_session
    else:
        client.sessions.get.return_value = old
        client.sessions.post_event.side_effect = change_session
    with pytest.raises(RuntimeError, match="Session changed during transition"):
        await adapter.start_new_conversation()
    assert adapter.session_id == "other"
    if phase == "read":
        client.sessions.post_event.assert_not_awaited()
    else:
        client.sessions.post_event.assert_awaited_once_with(
            "old", {"type": "stop_session", "data": {}}
        )
    client.sessions.unbind_runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_runner", [False, True])
async def test_remote_child_view_preserves_only_confirmed_shared_host(monkeypatch, shared_runner):
    import httpx
    from omnigent_client import OmnigentClient
    from omnigent_client._errors import OmnigentError

    from omnigent import chat
    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    base = "https://workspace.example/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "synthetic-token")
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.delenv(RUNNER_SLICE_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(chat, "_session_hosts", {})
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda url: None)
    monkeypatch.setattr("omnigent.telemetry.request_headers.telemetry_request_headers", dict)
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="local-host"),
    )
    location = {"host": "remote-host", "runner": "shared-runner"}
    requests = []

    def body(sid):
        return {
            "id": sid,
            "agent_id": "agent",
            "status": "idle",
            "created_at": 0,
            "runner_id": location["runner"]
            if sid == "parent" or shared_runner
            else "other-runner",
            "host_id": location["host"] if sid == "parent" else None,
            "workspace": "/remote" if sid == "parent" else None,
        }

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            expected = location["host"] if shared_runner else "local-host"
            assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == expected
            return (
                httpx.Response(202, json={"queued": True})
                if shared_runner
                else httpx.Response(
                    409, json={"error": {"code": "wrong_replica", "message": "host unknown"}}
                )
            )
        return httpx.Response(200, json=body(request.url.path.rsplit("/", 1)[1]))

    auth = chat._DatabricksTokenAuth(base, session_id="parent")
    async with OmnigentClient(base_url=base, auth=auth) as client:
        await client._http._transport.aclose()
        client._http._transport = httpx.MockTransport(handler)
        client._http._mounts.clear()
        adapter = _SessionsChatReplAdapter(
            client, "agent", session_id="parent", runner_id="shared-runner"
        )
        adapter._hydrate_from_session_snapshot(Session.from_dict(body("parent")))
        adapter._stream_pump = AsyncMock()
        try:
            await adapter.view_session("child", read_only=True, interactive=True)
            # Repeated hostless child snapshots keep the verified routing key.
            adapter._hydrate_from_session_snapshot(await client.sessions.get("child"))
            if shared_runner:
                await client.sessions.post_event("child", {"type": "message", "data": {}})
                location.update(host="moved-host", runner="replacement-runner")
                await adapter.view_session("parent", read_only=False)
                await adapter.view_session("child", read_only=True, interactive=True)
                await client.sessions.post_event("child", {"type": "message", "data": {}})
                assert chat.get_session_host("child") == "moved-host"
            else:
                with pytest.raises(OmnigentError):
                    await client.sessions.post_event("child", {"type": "message", "data": {}})
                assert chat.get_session_host("child") is None
        finally:
            await adapter.aclose()
    assert requests[0].headers[OMNIGENT_SLICE_KEY_HEADER] == "remote-host"
