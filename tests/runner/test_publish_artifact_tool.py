"""Native publishing uses session authority and stays inside its workspace."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_file_tool, build_native_relay_tool_schemas
from omnigent.tools.builtins.publish_artifact import PublishArtifactTool


@pytest.mark.asyncio
async def test_native_publish_uploads_expected_bytes_and_returns_link(tmp_path):
    (tmp_path / "report.html").write_text("<h1>Report</h1>")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/v1/sessions/" + "a" * 32 + "/artifacts"
        assert b"<h1>Report</h1>" in request.content
        assert b'name="kind"' in request.content
        return httpx.Response(
            201,
            json={
                "id": "b" * 32,
                "viewer_url": "/artifacts/" + "b" * 32,
                "download_url": "/v1/artifacts/" + "b" * 32 + "/content",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://server.example"
    ) as client:
        result = await _execute_file_tool(
            PublishArtifactTool.name(),
            {"path": "report.html", "title": "Report", "kind": "html"},
            client,
            conversation_id="a" * 32,
            runner_workspace=tmp_path,
        )
    assert len(seen) == 1
    assert json.loads(result)["viewer_url"] == "https://server.example/artifacts/" + "b" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../private.txt", "/etc/passwd", "outside-link"])
async def test_native_publishing_rejects_escape_without_upload(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "private.txt").write_text("private")
    (workspace / "outside-link").symlink_to(tmp_path / "private.txt")
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: seen.append(r)), base_url="https://server.example"
    ) as client:
        result = await _execute_file_tool(
            PublishArtifactTool.name(),
            {"path": path, "title": "x"},
            client,
            conversation_id="a" * 32,
            runner_workspace=workspace,
        )
    assert result.startswith("Error:")
    assert not seen


def test_native_default_surface_includes_publish():
    names = {schema["name"] for schema in build_native_relay_tool_schemas(None)}
    assert PublishArtifactTool.name() in names
