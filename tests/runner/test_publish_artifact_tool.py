"""Native publishing uses session authority and stays inside its workspace."""

import json
import os

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_file_tool,
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec.types import AgentSpec
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


@pytest.mark.parametrize("spec", [None, AgentSpec(spec_version=1)])
def test_native_default_surface_includes_publish(spec):
    names = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert PublishArtifactTool.name() in names


@pytest.mark.asyncio
async def test_copy_on_write_publishing_is_rejected_before_read_or_upload(tmp_path, monkeypatch):
    (tmp_path / "report.html").write_text("original host file")
    monkeypatch.setattr("omnigent.sandbox.copy_on_write.has_copy_on_write", lambda _: True)
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: seen.append(r)), base_url="https://server.example"
    ) as client:
        result = await execute_tool(
            tool_name=PublishArtifactTool.name(),
            arguments=json.dumps({"path": "report.html", "title": "Report"}),
            server_client=client,
            agent_spec=AgentSpec(spec_version=1),
            conversation_id="a" * 32,
            runner_workspace=tmp_path,
            effective_harness="pi-native",
        )
    assert "does not yet support copy_on_write" in result
    assert not seen


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
async def test_native_publishing_rejects_fifo_without_upload(tmp_path):
    os.mkfifo(tmp_path / "report.pipe")
    seen = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: seen.append(r)), base_url="https://server.example"
    ) as client:
        result = await _execute_file_tool(
            PublishArtifactTool.name(),
            {"path": "report.pipe", "title": "Report"},
            client,
            conversation_id="a" * 32,
            runner_workspace=tmp_path,
        )
    assert result.startswith("Error:")
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sandbox",
    [
        {"cwd_allow_hidden": []},
        {"mask_paths": ["secret.txt"]},
    ],
)
async def test_native_publishing_rejects_restricted_filesystem(tmp_path, sandbox):
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    (tmp_path / "secret.txt").write_text("masked private content")
    seen = []
    spec = AgentSpec(spec_version=1, os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(**sandbox)))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: seen.append(r)), base_url="https://server.example"
    ) as client:
        result = await _execute_file_tool(
            PublishArtifactTool.name(),
            {"path": "secret.txt", "title": "Report"},
            client,
            conversation_id="a" * 32,
            runner_workspace=tmp_path,
            agent_spec=spec,
        )
    assert "restricted OS environments" in result
    assert not seen


@pytest.mark.skipif(os.name != "posix", reason="Confined directory opens require POSIX")
@pytest.mark.parametrize("replacement", ["file", "parent"])
def test_publication_read_rejects_symlink_replacement(tmp_path, monkeypatch, replacement):
    from omnigent.runner.published_artifact_file import read_publication_file

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "reports").mkdir()
    (root / "reports" / "report.txt").write_text("public")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "report.txt").write_text("private")
    original_open = os.open
    swapped = []

    def racing_open(path, flags, *args, **kwargs):
        if path == ("report.txt" if replacement == "file" else "reports") and not swapped:
            swapped.append(True)
            target = root / "reports" / "report.txt" if replacement == "file" else root / "reports"
            target.rename(target.with_name(target.name + ".old"))
            target.symlink_to(outside / "report.txt" if replacement == "file" else outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, racing_open})
    with pytest.raises(OSError):
        read_publication_file("reports/report.txt", root, 100)
    assert swapped


@pytest.mark.skipif(os.name != "posix", reason="Confined directory opens require POSIX")
def test_publication_rejects_replaced_workspace_and_bounds_content(tmp_path):
    from omnigent.runner.published_artifact_file import read_publication_file

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.txt").write_bytes(b"1234")
    with pytest.raises(ValueError, match="limit"):
        read_publication_file("report.txt", root, 3)
    assert read_publication_file("report.txt", root, 4) == ("report.txt", b"1234")
    root.rename(tmp_path / "old-workspace")
    root.symlink_to(tmp_path / "old-workspace")
    with pytest.raises(OSError):
        read_publication_file("report.txt", root, 4)
