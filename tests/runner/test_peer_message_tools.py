"""Peer tools retain authenticated session binding on native and runner paths."""

import json
import uuid

import httpx
import pytest

from omnigent.runner.peer_messages import execute_peer_tool
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, should_dispatch_locally
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.peer_messages import PEER_TOOL_NAMES
from omnigent.tools.manager import ToolManager


def test_peer_tools_registered_and_relayed_without_spawn():
    spec = AgentSpec(spec_version=1)
    schemas = ToolManager(spec, os_env_schema_only=True).get_tool_schemas()
    assert {schema["function"]["name"] for schema in schemas} >= PEER_TOOL_NAMES
    for candidate in (None, spec):
        assert {
            schema["name"] for schema in build_native_relay_tool_schemas(candidate)
        } >= PEER_TOOL_NAMES
    for name in PEER_TOOL_NAMES:
        assert should_dispatch_locally(name)


@pytest.mark.asyncio
async def test_session_bound_send_read_ack_and_retry_id():
    source, target, identifier = [uuid.uuid4().hex for _ in range(3)]
    requests = []

    def transport(request):
        requests.append(request)
        payload = json.loads(request.content) if request.content else {}
        return httpx.Response(201, json={"ok": True, "id": payload.get("id")})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="https://example.test"
    ) as client:
        args = {
            "recipient_session_id": target,
            "subject": "hello",
            "body": "context",
            "message_id": identifier,
        }
        sent = json.loads(await execute_peer_tool("sys_peer_send", args, source, client))
        assert sent["message_id"] == identifier
        assert json.loads(requests[-1].content) == {
            "id": identifier,
            "recipient_session_id": target,
            "subject": "hello",
            "body": "context",
        }
        assert str(requests[-1].url).endswith(f"/sessions/{source}/peer-messages")
        await execute_peer_tool("sys_peer_read", {"unread_only": True}, source, client)
        assert requests[-1].url.params["unread_only"] == "true"
        await execute_peer_tool("sys_peer_acknowledge", {"message_id": identifier}, source, client)
        assert str(requests[-1].url).endswith(f"/{identifier}/acknowledge")
        count = len(requests)
        assert "error" in json.loads(
            await execute_peer_tool(
                "sys_peer_acknowledge", {"message_id": "../permissions"}, source, client
            )
        )
        assert len(requests) == count


@pytest.mark.asyncio
async def test_uncertain_send_returns_reusable_id_without_retry():
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("response interrupted", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="https://example.test"
    ) as client:
        result = json.loads(
            await execute_peer_tool(
                "sys_peer_send",
                {"recipient_session_id": uuid.uuid4().hex, "subject": "test", "body": "content"},
                uuid.uuid4().hex,
                client,
            )
        )
        assert result["outcome_unknown"] is True
        assert len(result["message_id"]) == 32
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 504, 408])
async def test_ambiguous_http_send_preserves_id_and_bounded_detail(status):
    identifier = uuid.uuid4().hex
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="upstream timeout " * 1000)
        ),
        base_url="https://example.test",
    ) as client:
        result = json.loads(
            await execute_peer_tool(
                "sys_peer_send",
                {
                    "recipient_session_id": uuid.uuid4().hex,
                    "subject": "test",
                    "body": "hello",
                    "message_id": identifier,
                },
                uuid.uuid4().hex,
                client,
            )
        )
        assert result["outcome_unknown"] is True
        assert result["message_id"] == identifier
        assert len(result["server_detail"]) == 1024
        assert result["server_detail"].startswith("upstream timeout")


@pytest.mark.asyncio
async def test_invalid_success_body_is_uncertain_but_validation_rejection_is_definite():
    for status, body, unknown in (
        (201, "not valid JSON", True),
        (201, '{"unexpected":"response"}', True),
        (422, '{"detail":"Message body exceeds 8192 UTF-8 bytes"}', False),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, status=status, body=body: httpx.Response(status, text=body)
            ),
            base_url="https://example.test",
        ) as client:
            result = json.loads(
                await execute_peer_tool(
                    "sys_peer_send",
                    {
                        "recipient_session_id": uuid.uuid4().hex,
                        "subject": "test",
                        "body": "hello",
                    },
                    uuid.uuid4().hex,
                    client,
                )
            )
            assert result["outcome_unknown"] is unknown
            assert result["server_detail"] == body
            assert len(result["message_id"]) == 32


@pytest.mark.asyncio
async def test_runner_peer_notification_requires_pi_context_receipt(tmp_path, monkeypatch):
    from omnigent.harnesses.pi_native import bridge
    from omnigent.runner import create_runner_app
    from omnigent.spec.types import ExecutorSpec
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient

    sid = uuid.uuid4().hex
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    spec = AgentSpec(
        spec_version=1,
        name="pi",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def resolve(agent_id, session_id=None):
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        spec_resolver=resolve,
        server_client=NullServerClient(),
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert response.status_code == 201, response.text
        path = f"/v1/sessions/{sid}/events"
        payload = {
            "type": "peer_notification",
            "notification_id": "a" * 64,
            "message_ids": ["b" * 32],
        }
        assert (await client.post(path, json=payload)).status_code == 503
        directory = bridge.bridge_dir_for_session_id(sid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input_ready").touch()
        assert (await client.post(path, json=payload)).status_code == 202
        assert (await client.post(path, json=payload)).status_code == 202
        assert len(list((directory / "inbox").glob("*.json"))) == 1
        (directory / "peer-receipts").mkdir()
        (directory / "peer-receipts" / payload["notification_id"]).write_text("accepted\n")
        response = await client.post(path, json=payload)
        assert response.status_code == 200 and response.json()["delivered"]
        payload["notification_id"] = "../bad"
        assert (await client.post(path, json=payload)).status_code == 400
