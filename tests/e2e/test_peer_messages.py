"""Native peer tools reach production routes without waking an offline recipient."""

import asyncio
import json
import uuid

import httpx
import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from tests.e2e.conftest import (
    _live_runner_state,
    create_runner_bound_session,
    register_inline_agent,
)


@pytest.mark.asyncio
async def test_peer_send_reply_acknowledge_through_live_server(
    http_client, live_runner_id, restart_live_runner
):
    name = register_inline_agent(
        http_client,
        name=f"peer-{uuid.uuid4().hex[:8]}",
        harness="pi-native",
        model="test-model",
        profile="",
        prompt="Coordinate through your peer inbox.",
    )
    sessions = []
    runner_stopped = False
    try:
        for _ in range(2):
            sessions.append(
                create_runner_bound_session(http_client, agent_name=name, runner_id=live_runner_id)
            )
        source, target = sessions
        runner = _live_runner_state["process"]
        runner.kill()
        runner.wait(timeout=10)
        runner_stopped = True
        spec = AgentSpec(spec_version=1)
        assert {tool["name"] for tool in build_native_relay_tool_schemas(spec)} >= {
            "sys_peer_send",
            "sys_peer_read",
            "sys_peer_acknowledge",
        }
        async with httpx.AsyncClient(
            base_url=str(http_client.base_url), headers=dict(http_client.headers), trust_env=False
        ) as api:
            for _ in range(100):
                status = await api.get(f"/v1/runners/{live_runner_id}/status")
                if status.status_code == 200 and status.json().get("online") is False:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Stopped runner remained online")
            before = (await api.get(f"/v1/sessions/{target}")).json()
            assert before.get("runner_online") is False

            async def call(sid, tool, args):
                result = json.loads(
                    await execute_tool(
                        tool_name=tool,
                        arguments=json.dumps(args),
                        server_client=api,
                        agent_spec=spec,
                        conversation_id=sid,
                        effective_harness="pi-native",
                    )
                )
                assert "error" not in result, result
                return result

            sent = await call(
                source,
                "sys_peer_send",
                {
                    "recipient_session_id": target,
                    "subject": "Review ready",
                    "body": "Please inspect the report.",
                },
            )
            incoming = await call(target, "sys_peer_read", {"unread_only": True})
            message = incoming["result"]["messages"][0]
            assert message["id"] == sent["message_id"]
            assert message["sender_session_id"] == source
            await call(
                target,
                "sys_peer_send",
                {
                    "recipient_session_id": source,
                    "subject": "Review result",
                    "body": "Report checked.",
                    "reply_to": message["id"],
                },
            )
            reply = await call(source, "sys_peer_read", {})
            assert reply["result"]["messages"][0]["reply_to"] == message["id"]
            await call(target, "sys_peer_acknowledge", {"message_id": message["id"]})
            unread = await call(target, "sys_peer_read", {"unread_only": True})
            assert unread["result"]["messages"] == []
            # A separate HTTP read observes committed state, independent of tool-local state.
            persisted = (await api.get(f"/v1/sessions/{target}/peer-messages")).json()
            assert persisted["messages"][0]["acknowledged_at"] is not None
            after = (await api.get(f"/v1/sessions/{target}")).json()
            assert after.get("runner_online") is False
            for field in ("runner_id", "host_id", "sandbox_stage", "status"):
                assert after.get(field) == before.get(field)
    finally:
        for sid in sessions:
            http_client.delete(f"/v1/sessions/{sid}").raise_for_status()
        if runner_stopped:
            restart_live_runner()
