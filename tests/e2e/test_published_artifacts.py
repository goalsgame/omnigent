"""Native publication discovery and HTTP delivery through a real server process."""

import json
import uuid

import httpx
import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


@pytest.mark.asyncio
async def test_native_publication_survives_source_session_deletion(
    http_client: httpx.Client, live_runner_id: str, tmp_path
):
    """A resolved native spec discovers the tool and publishes immutable workspace bytes."""
    name = register_inline_agent(
        http_client,
        name=f"publisher-{uuid.uuid4().hex[:8]}",
        harness="pi-native",
        model="test-model",
        profile="",
        prompt="Publish reports.",
    )
    sid = create_runner_bound_session(http_client, agent_name=name, runner_id=live_runner_id)
    spec = AgentSpec(spec_version=1)
    assert "sys_publish_artifact" in {
        tool["name"] for tool in build_native_relay_tool_schemas(spec)
    }
    content = b"<!doctype html><h1>Published from a native session</h1>"
    (tmp_path / "report.html").write_bytes(content)
    publication = None
    try:
        async with httpx.AsyncClient(
            base_url=str(http_client.base_url), headers=dict(http_client.headers), trust_env=False
        ) as api:
            result = await execute_tool(
                tool_name="sys_publish_artifact",
                arguments=json.dumps(
                    {"path": "report.html", "title": "Native report", "kind": "html"}
                ),
                server_client=api,
                agent_spec=spec,
                conversation_id=sid,
                runner_workspace=tmp_path,
                effective_harness="pi-native",
            )
            publication = json.loads(result)
            assert "id" in publication, result
            first = await api.get(publication["download_url"])
            assert first.status_code == 200
            assert first.content == content
            preview = await api.get(f"/v1/artifacts/{publication['id']}/preview")
            assert preview.content == content
            assert preview.headers["content-type"].startswith("text/plain")
            assert preview.headers["content-disposition"].startswith("attachment;")
            assert preview.headers["content-security-policy"] == "default-src 'none'; sandbox"
            async with httpx.AsyncClient(
                base_url=str(http_client.base_url), trust_env=False
            ) as public:
                renderer = await public.get("/v1/artifact-renderer")
                assert renderer.status_code == 200
                assert "frame-src 'none'" in renderer.headers["content-security-policy"]
                assert content not in renderer.content
            deleted = await api.delete(f"/v1/sessions/{sid}")
            deleted.raise_for_status()
            after = await api.get(publication["download_url"])
            assert after.status_code == 200
            assert after.content == content
            metadata = await api.get(f"/v1/artifacts/{publication['id']}")
            assert metadata.json()["source_session_id"] == sid
    finally:
        if publication and "id" in publication:
            http_client.delete(f"/v1/artifacts/{publication['id']}").raise_for_status()
        http_client.delete(f"/v1/sessions/{sid}")
