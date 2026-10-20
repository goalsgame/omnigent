"""Tool RPCs retain server enforcement and use renewable runner authentication."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import start_tool_relay
from omnigent.harnesses.pi_native import bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [200, 401, 403, 503])
async def test_mcp_relay_preserves_rpc_and_uses_fresh_session_bound_auth(
    tmp_path, monkeypatch, upstream_status
):
    monkeypatch.delenv("OMNIGENT_BRIDGE_BIND_HOST", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = bridge.prepare_bridge_dir("relay-session")
    seen = []

    class FreshAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = f"Bearer refreshed-{len(seen)}"
            yield request

    response_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"resultType": "input_required", "requestState": "opaque-state"},
    }

    def upstream(request):
        assert request.url.path == "/v1/sessions/relay-session/mcp"
        assert request.headers["Authorization"] == f"Bearer refreshed-{len(seen)}"
        assert "X-Expired-Credential" not in request.headers
        seen.append(json.loads(request.content))
        return httpx.Response(upstream_status, json=response_body)

    async with httpx.AsyncClient(
        base_url="https://server.example",
        transport=httpx.MockTransport(upstream),
        auth=FreshAuth(),
    ) as runner_client:
        executor = AsyncMock()
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=executor,
            loop=asyncio.get_running_loop(),
            policy_client=runner_client,
            session_id="relay-session",
        )
        try:
            info = json.loads((bridge_dir / "tool_relay.json").read_text())
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sys_peer_send",
                    "arguments": {"recipient_session_id": "other"},
                },
            }
            async with httpx.AsyncClient(trust_env=False) as local:
                url = info["url"] + "/mcp"
                refused = await local.post(url, json=body)
                assert refused.status_code == 401
                assert not seen
                headers = {"Authorization": "Bearer " + info["token"]}
                for method in ("tools/list", "initialize", "unknown"):
                    refused = await local.post(
                        url, headers=headers, json={**body, "method": method}
                    )
                    assert refused.status_code == 400
                assert not seen
                for params in (
                    body["params"],
                    {
                        **body["params"],
                        "requestState": "opaque-state",
                        "inputResponses": {"approval": {"action": "decline"}},
                    },
                ):
                    sent = {**body, "params": params, "session_id": "cannot-change-relay-session"}
                    response = await local.post(url, headers=headers, json=sent)
                    assert response.status_code == upstream_status
                    assert response.json() == response_body
                    assert int(response.headers["Content-Length"]) == len(response.content)
                    assert seen[-1] == sent
                assert len(seen) == 2
                executor.assert_not_called()
        finally:
            relay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_mcp_relay_renews_rejected_bearer_and_fails_closed_on_outage(
    tmp_path, monkeypatch, unavailable
):
    from omnigent.runner._entry import _RunnerDatabricksAuth

    monkeypatch.delenv("OMNIGENT_BRIDGE_BIND_HOST", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = bridge.prepare_bridge_dir("relay-session")
    seen = []

    def credential():
        return "expired" if not seen else "renewed"

    def upstream(request):
        seen.append(request.headers["Authorization"])
        if unavailable:
            raise httpx.ConnectError("provider unavailable", request=request)
        if len(seen) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    async with httpx.AsyncClient(
        base_url="https://server.example",
        transport=httpx.MockTransport(upstream),
        auth=_RunnerDatabricksAuth(credential, "https://server.example"),
    ) as runner_client:
        executor = AsyncMock()
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=executor,
            loop=asyncio.get_running_loop(),
            policy_client=runner_client,
            session_id="relay-session",
        )
        try:
            info = json.loads((bridge_dir / "tool_relay.json").read_text())
            async with httpx.AsyncClient(trust_env=False) as local:
                response = await local.post(
                    info["url"] + "/mcp",
                    headers={"Authorization": "Bearer " + info["token"]},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
                )
            assert response.status_code == (502 if unavailable else 200)
            assert seen == (
                ["Bearer expired"] if unavailable else ["Bearer expired", "Bearer renewed"]
            )
            executor.assert_not_called()
        finally:
            relay.close()


@pytest.mark.asyncio
async def test_peer_actor_relay_is_authenticated_session_bound_and_uses_fresh_auth(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OMNIGENT_BRIDGE_BIND_HOST", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = bridge.prepare_bridge_dir("bound-session")
    seen = []

    class FreshAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = f"Bearer renewed-{len(seen)}"
            yield request

    def upstream(request):
        assert request.url.path == "/v1/sessions/bound-session/peer-notifications/actor"
        assert request.headers["Authorization"] == f"Bearer renewed-{len(seen)}"
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://server.example",
        transport=httpx.MockTransport(upstream),
        auth=FreshAuth(),
    ) as runner_client:
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=AsyncMock(),
            loop=asyncio.get_running_loop(),
            policy_client=runner_client,
            session_id="bound-session",
        )
        try:
            info = json.loads((bridge_dir / "tool_relay.json").read_text())
            async with httpx.AsyncClient(trust_env=False) as client:
                url = info["url"] + "/peer-notifications/actor"
                body = {"notification_id": "a" * 64}
                assert (await client.post(url, json=body)).status_code == 401
                headers = {"Authorization": "Bearer " + info["token"]}
                assert (
                    await client.post(url, headers=headers, content=b"x" * 1025)
                ).status_code == 400
                for _ in range(2):
                    assert (await client.post(url, headers=headers, json=body)).status_code == 200
                assert seen == [body, body]
        finally:
            relay.close()
