"""The session search tool is offered only when enabled and always searches for its own session."""

import json
import uuid

import httpx

from omnigent.runner.session_search import execute_session_search_tool
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, should_dispatch_locally
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.session_search import SearchSessionsTool
from omnigent.tools.manager import ToolManager

NAME = SearchSessionsTool.name()


def _names(spec):
    return {
        s["function"]["name"]
        for s in ToolManager(spec, os_env_schema_only=True).get_tool_schemas()
    }


def test_tool_follows_deployment_configuration(monkeypatch):
    spec = AgentSpec(spec_version=1)
    monkeypatch.delenv("OMNIGENT_SESSION_SEARCH_TOOL", raising=False)
    assert NAME not in _names(spec)
    for candidate in (None, spec):
        assert NAME not in {s["name"] for s in build_native_relay_tool_schemas(candidate)}

    monkeypatch.setenv("OMNIGENT_SESSION_SEARCH_TOOL", "1")
    assert NAME in _names(spec)
    for candidate in (None, spec):
        assert NAME in {s["name"] for s in build_native_relay_tool_schemas(candidate)}
    assert should_dispatch_locally(NAME)


def test_schema_uses_single_json_types():
    properties = SearchSessionsTool().get_schema()["function"]["parameters"]["properties"]
    assert {name: spec["type"] for name, spec in properties.items()} == {
        "query": "string",
        "limit": "integer",
    }


async def test_requests_are_bound_to_the_session_and_report_errors():
    session = uuid.uuid4().hex
    requests = []
    hit = {"session_id": "a" * 32, "title": "Rollout", "matches": []}

    def transport(request):
        requests.append(request)
        if request.url.params["q"] == "broken":
            return httpx.Response(
                400, json={"error": {"message": "Search query must not be empty"}}
            )
        if request.url.params["q"] == "nothing":
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
        return httpx.Response(200, json={"object": "list", "data": [hit], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="https://example.test"
    ) as client:

        async def run(args, conversation_id=session):
            return json.loads(await execute_session_search_tool(args, conversation_id, client))

        assert (await run({"query": "warm pool", "limit": 5}))["sessions"] == [hit]
        assert requests[-1].url.path == f"/v1/sessions/{session}/search"
        assert requests[-1].url.params["limit"] == "5"

        assert (await run({"query": "nothing"}))["sessions"] == []
        assert await run({"query": "broken"}) == {"error": "Search query must not be empty"}

        count = len(requests)
        for bad in ({"query": ""}, {"query": "x", "limit": 0}, {"query": "x", "limit": True}):
            assert "error" in await run(bad)
        assert "error" in await run({"query": "x"}, conversation_id="../permissions")
        assert len(requests) == count

    assert "error" in json.loads(await execute_session_search_tool({"query": "x"}, session, None))
