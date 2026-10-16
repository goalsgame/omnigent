"""Real backends serve each other's runner traffic and recover its placement."""

import json

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.stores.backend_route_store import BackendRouteStore
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
)
from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _gate_pending,
    _poll_until,
    _ReconnectStack,
    _Replica,
    _send_user_message,
    _session_blob,
    _terminate,
    _TunnelIngressProxy,
)


def test_other_replica_serves_turn_and_owner_rollout_preserves_runner_work(
    mock_llm_server_url, tmp_path
):
    stack = _ReconnectStack(mock_llm_server_url, tmp_path, replication=True)
    b = None
    replacement = None
    try:
        stack.start()
        b = stack.start_second_replica()
        observer = BackendRouteStore(stack._database_uri, "observer", "http://127.0.0.1:1")
        original = observer.get("runner", stack.runner_id)
        assert original.endpoint == stack.base_url
        # A healthy status through B must come from the runner tunnel on A.
        assert b.runner_online()
        listed = b.client.get("/v1/runners")
        assert listed.status_code == 200, listed.text
        assert stack.runner_id in {row["runner_id"] for row in listed.json()["data"]}
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [
                {"text": "REPLICA_ORIGINAL_TURN_DONE", "block": True},
                {"text": "REPLICA_FOLLOWUP_DONE"},
            ],
        )
        agent = register_inline_agent(
            b.client,
            name="replica-routing-test",
            harness="openai-agents",
            model="replica-routing-test",
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        sid = create_runner_bound_session(b.client, agent_name=agent, runner_id=stack.runner_id)
        _send_user_message(b.client, sid)
        _poll_until(
            lambda: _gate_pending(mock_llm_server_url),
            timeout=30,
            what="the turn sent through B to run on A's runner",
        )
        # The model runs in the independent runner process. Stop its owning
        # backend while it is working and reconnect its transport to B.
        stack.proxy.retarget("127.0.0.1", b.port)
        _terminate(stack._server_proc)

        def moved():
            route = observer.get("runner", stack.runner_id)
            return route is not None and route.endpoint == b.base_url and b.runner_online()

        _poll_until(moved, timeout=45, what="the same runner to reconnect to B")
        release_mock_gate(mock_llm_server_url)
        _poll_until(
            lambda: "REPLICA_ORIGINAL_TURN_DONE" in _session_blob(b.client, sid),
            timeout=45,
            what="the original turn to finish after backend handover",
        )
        # Bring another backend up and send the next turn through it, proving
        # the replacement serves traffic for the survivor rather than stealing it.
        directory = tmp_path / "replacement"
        directory.mkdir()
        replacement = _Replica(stack, directory)
        replacement.start()
        assert replacement.runner_online()
        _send_user_message(replacement.client, sid, "Finish the follow-up.")
        _poll_until(
            lambda: "REPLICA_FOLLOWUP_DONE" in _session_blob(replacement.client, sid),
            timeout=45,
            what="the follow-up turn through the replacement backend",
        )
        snapshot = replacement.client.get(f"/v1/sessions/{sid}").json()
        assert snapshot.get("status") != "failed", json.dumps(snapshot)
        assert "runner_disconnected" not in json.dumps(snapshot)
        denied = replacement.client.get(
            f"/v1/sessions/{sid}", headers={"X-Forwarded-Email": "another-user@example.com"}
        )
        assert denied.status_code in (403, 404), denied.text
    finally:
        release_mock_gate(mock_llm_server_url)
        if replacement:
            replacement.teardown()
        if b:
            b.teardown()
        stack.teardown()


def test_host_operations_and_new_runner_reach_the_host_owner(mock_llm_server_url, tmp_path):
    stack = _ReconnectStack(mock_llm_server_url, tmp_path, replication=True)
    b = None
    host = None
    ingress = None
    try:
        stack.start()
        b = stack.start_second_replica()
        ingress = _TunnelIngressProxy("127.0.0.1", stack._port)
        workspace = tmp_path / "host-workspace"
        workspace.mkdir()
        (workspace / "cross-replica.txt").write_text("shared host")
        host = _spawn_host_daemon(
            tmp_path=workspace,
            live_server=f"http://127.0.0.1:{ingress.port}",
            mock_llm_server_url=mock_llm_server_url,
            interactive_shells=["zsh"],
        )
        _wait_for_host_online(b.client, host.host_id, timeout=30)
        hosts = b.client.get("/v1/hosts").json()["hosts"]
        assert next(row for row in hosts if row["host_id"] == host.host_id)[
            "interactive_shells"
        ] == ["zsh"]
        listing = b.client.get(f"/v1/hosts/{host.host_id}/filesystem/{workspace}")
        assert listing.status_code == 200, listing.text
        assert "cross-replica.txt" in listing.text
        # Keep the host tunnel on A, but send newly launched runner connections
        # to B. B must authenticate and forward their tunnel to the host owner.
        ingress.retarget("127.0.0.1", b.port)
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(mock_llm_server_url, [{"text": "HOST_REPLICA_TURN_DONE"}])
        name = register_inline_agent(
            b.client,
            name="host-replica-routing",
            harness="openai-agents",
            model="host-replica-routing",
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        created = b.client.post(
            "/v1/sessions",
            json={
                "agent_id": lookup_agent_id(b.client, name),
                "host_id": host.host_id,
                "workspace": str(workspace),
            },
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=60,
        )
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        _poll_until(
            lambda: b.client.get(f"/v1/sessions/{sid}").json().get("runner_id") is not None,
            timeout=30,
            what="the session's host-launched runner binding",
        )
        rid = b.client.get(f"/v1/sessions/{sid}").json()["runner_id"]
        _poll_until(
            lambda: b.client.get(f"/v1/runners/{rid}/status").json().get("online"),
            timeout=30,
            what="the runner sent through B to connect on A",
        )
        _send_user_message(b.client, sid)
        _poll_until(
            lambda: "HOST_REPLICA_TURN_DONE" in _session_blob(b.client, sid),
            timeout=45,
            what="the host-backed turn through the non-owning backend",
        )
    finally:
        if host:
            _terminate(host.proc)
        if ingress:
            ingress.close()
        if b:
            b.teardown()
        stack.teardown()
