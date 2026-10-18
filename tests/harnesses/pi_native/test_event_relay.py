"""Native events use session-bound runner authentication rather than launch bearers."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import start_tool_relay
from omnigent.harnesses.pi_native import bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [202, 401, 503])
async def test_events_relay_uses_runner_client_and_fixed_session(
    tmp_path, monkeypatch, upstream_status
):
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    monkeypatch.delenv("OMNIGENT_BRIDGE_BIND_HOST", raising=False)
    bridge_dir = bridge.prepare_bridge_dir("relay-session")
    seen = []

    class FreshAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer refreshed-runner-token"
            yield request

    def upstream(request):
        assert request.url.path == "/v1/sessions/relay-session/events"
        assert request.headers["Authorization"] == "Bearer refreshed-runner-token"
        seen.append(json.loads(request.content))
        return httpx.Response(upstream_status, json={"sensitive": "not-forwarded"})

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
            session_id="relay-session",
        )
        try:
            info = json.loads((bridge_dir / "tool_relay.json").read_text())
            body = {"type": "external_conversation_item", "data": {"source_id": "pi-message"}}
            async with httpx.AsyncClient(trust_env=False) as local:
                url = info["url"] + "/events"
                refused = await local.post(url, json=body)
                assert refused.status_code == 401
                assert not seen
                headers = {"Authorization": "Bearer " + info["token"]}
                for invalid in ("message", "stop_session", {"bad": "type"}):
                    refused = await local.post(
                        url, headers=headers, json={"type": invalid, "data": {}}
                    )
                    assert refused.status_code == 400
                assert not seen
                response = await local.post(url, headers=headers, json=body)
                assert response.status_code == upstream_status
                assert response.json() == {}
                assert seen == [body]
        finally:
            relay.close()
