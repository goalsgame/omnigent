"""Each turn hands the harness the agent's current MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, MCPServerConfig
from tests.runner.conftest import (
    _FakeMcpManager,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "6f1c2b3a4d5e6f708192a3b4c5d6e7f8"
_AGENT_ID = "1a2b3c4d5e6f708192a3b4c5d6e7f809"


class _ChangingToolsServerClient(NullServerClient):
    """Server client whose MCP proxy lists ``listings[0]`` until a test pops it."""

    def __init__(self, listings: list[list[str]]) -> None:
        self.listings = listings

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        body = kwargs.get("json")
        if url.endswith("/mcp") and isinstance(body, dict) and body.get("method") == "tools/list":
            names = self.listings[0]

            class _ToolsListResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {
                        "result": {
                            "tools": [
                                {
                                    "name": name,
                                    "description": name,
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                                for name in names
                            ]
                        }
                    }

            return _ToolsListResponse()
        return await super().post(url, **kwargs)


async def _send_turn(client: Any, harness_client: _ScriptedHarnessClient, turn: int) -> list[str]:
    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": _AGENT_ID,
            "model": "test-agent",
            "input": [{"type": "input_text", "text": f"turn {turn}"}],
            "harness": "openai-agents",
            "has_mcp_servers": True,
        },
    )
    assert resp.status_code == 202, resp.text
    async with asyncio.timeout(10):
        while len(harness_client.posted_bodies) < turn:
            await asyncio.sleep(0.01)
    tools = harness_client.posted_bodies[turn - 1].get("tools") or []
    return [str(tool["name"]) for tool in tools if str(tool.get("name")).startswith("jira__")]


@pytest.mark.asyncio
async def test_next_turn_sees_added_and_not_removed_mcp_tools() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        mcp_servers=[MCPServerConfig(name="jira", transport="http", url="http://x")],
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    server_client = _ChangingToolsServerClient(
        [["jira__alpha", "jira__gamma"], ["jira__alpha", "jira__beta"]]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        mcp_manager=_FakeMcpManager(),
    )
    async with _runner_client(app) as client:
        first = await _send_turn(client, harness_client, 1)
        assert {"jira__alpha", "jira__gamma"} <= set(first)
        await asyncio.sleep(0.1)

        server_client.listings.pop(0)
        second = await _send_turn(client, harness_client, 2)

    assert "jira__beta" in second
    assert "jira__alpha" in second
    assert "jira__gamma" not in second
    assert len(second) == len(set(second))


def test_failed_servers_keep_their_previous_mcp_tools() -> None:
    from omnigent.runner.app import _MCP_LISTING_FAILED, _merge_mcp_listing

    previous = [{"name": "jira__alpha"}, {"name": "github__search"}]
    fresh = [{"name": "jira__alpha"}, {"name": "jira__beta"}]

    assert _merge_mcp_listing(previous, fresh, {}) == fresh
    assert _merge_mcp_listing(previous, fresh, {"github": "down"}) == [
        *fresh,
        {"name": "github__search"},
    ]
    assert _merge_mcp_listing(previous, [], {_MCP_LISTING_FAILED: "HTTPError"}) == previous
    assert _merge_mcp_listing(previous, [], {"proxy": "unavailable"}) == []
