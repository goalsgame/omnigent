"""Google Drive tools are offered only when configured and always act on the runner's session."""

import json
import uuid

import httpx

from omnigent.runner.google_drive import execute_google_drive_tool
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, should_dispatch_locally
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.google_drive import GOOGLE_DRIVE_TOOL_NAMES
from omnigent.tools.manager import ToolManager


def _names(spec):
    return {
        s["function"]["name"]
        for s in ToolManager(spec, os_env_schema_only=True).get_tool_schemas()
    }


def test_tools_follow_deployment_configuration(monkeypatch):
    spec = AgentSpec(spec_version=1)
    monkeypatch.delenv("OMNIGENT_GOOGLE_DRIVE_AUTH", raising=False)
    assert not _names(spec) & GOOGLE_DRIVE_TOOL_NAMES
    for candidate in (None, spec):
        relayed = {s["name"] for s in build_native_relay_tool_schemas(candidate)}
        assert not relayed & GOOGLE_DRIVE_TOOL_NAMES

    monkeypatch.setenv("OMNIGENT_GOOGLE_DRIVE_AUTH", "1")
    assert _names(spec) >= GOOGLE_DRIVE_TOOL_NAMES
    for candidate in (None, spec):
        relayed = {s["name"] for s in build_native_relay_tool_schemas(candidate)}
        assert relayed >= GOOGLE_DRIVE_TOOL_NAMES
    for name in GOOGLE_DRIVE_TOOL_NAMES:
        assert should_dispatch_locally(name)


async def test_requests_are_bound_to_the_session_and_report_errors():
    session = uuid.uuid4().hex
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path.endswith("/append"):
            return httpx.Response(403, json={"detail": "Ask a participant to add it"})
        if request.method == "PUT":
            return httpx.Response(422, json={"detail": [{"msg": "range too long"}]})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="https://example.test"
    ) as client:
        base = f"/v1/sessions/{session}/google-drive/agent/files"

        async def run(tool, args):
            return json.loads(await execute_google_drive_tool(tool, args, session, client))

        assert await run("google_drive_list", {"query": "plan", "limit": 5}) == {"ok": True}
        assert requests[-1].url.path == base
        assert requests[-1].url.params["query"] == "plan"

        await run("google_drive_read", {"file_id": "doc1234567", "range": "A1:B2"})
        assert requests[-1].url.path == f"{base}/doc1234567"
        assert requests[-1].url.params["range"] == "A1:B2"

        await run("google_drive_create", {"title": "T", "kind": "document"})
        assert requests[-1].method == "POST" and requests[-1].url.path == base

        assert await run("google_docs_append", {"file_id": "doc1234567", "text": "x"}) == {
            "error": "Ask a participant to add it"
        }
        result = await run(
            "google_sheets_update", {"file_id": "doc1234567", "range": "A1", "values": [[1]]}
        )
        assert result == {"error": "Invalid arguments: range too long"}

        count = len(requests)
        bad = await run("google_drive_read", {"file_id": "../../permissions"})
        assert "error" in bad
        assert len(requests) == count


async def test_requires_a_server_session():
    result = json.loads(await execute_google_drive_tool("google_drive_list", {}, None, None))
    assert "error" in result


def test_schemas_use_single_types_for_gemini():
    from omnigent.tools.builtins.google_drive import GOOGLE_DRIVE_TOOL_TYPES

    def types(node):
        if isinstance(node, dict):
            if "type" in node:
                yield node["type"]
            for value in node.values():
                yield from types(value)
        elif isinstance(node, list):
            for value in node:
                yield from types(value)

    for tool in GOOGLE_DRIVE_TOOL_TYPES:
        assert all(isinstance(t, str) for t in types(tool().get_schema())), tool.name()
