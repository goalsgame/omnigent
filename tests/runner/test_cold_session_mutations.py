"""Cold filesystem and MCP mutations must use the selected session workspace."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [False, True])
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "MCP"])
async def test_cold_mutation_targets_selected_workspace_and_records_diff(
    tmp_path: Path, isolate: bool, method: str
) -> None:
    launch = tmp_path / "launch"
    selected = tmp_path / "selected"
    launch.mkdir()
    expected = selected / "cold-session" if isolate else selected
    expected.mkdir(parents=True)
    (launch / "note.txt").write_text("runner-original")
    (expected / "note.txt").write_text("project-original")
    spec = AgentSpec(
        spec_version=1, os_env=OSEnvSpec(cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/cold-session":
            return httpx.Response(
                200,
                json={
                    "id": "cold-session",
                    "agent_id": "test-agent",
                    "workspace": str(selected),
                },
            )
        return httpx.Response(404, json={})

    registry = SessionResourceRegistry(runner_workspace=launch, per_session_workspace=isolate)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata), base_url="http://server"
    ) as server:
        app = create_runner_app(
            server_client=server,
            spec_resolver=resolve if method == "MCP" else None,
            resource_registry=registry,
            runner_workspace=launch,
            per_session_workspace=isolate,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                base = f"/v1/sessions/cold-session/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
                if method == "MCP":
                    response = await client.post(
                        "/v1/sessions/cold-session/mcp/execute",
                        json={
                            "method": "tools/call",
                            "params": {
                                "name": "sys_os_write",
                                "arguments": {"path": "note.txt", "content": "updated"},
                            },
                        },
                    )
                    assert "error" not in response.json(), response.text
                else:
                    body = (
                        {"content": "updated"}
                        if method == "PUT"
                        else {
                            "old_text": "project-original",
                            "new_text": "updated",
                        }
                    )
                    response = await client.request(
                        method,
                        f"{base}/filesystem/note.txt",
                        json=body if method != "DELETE" else None,
                    )
                assert response.status_code == 200, response.text
                assert (launch / "note.txt").read_text() == "runner-original"
                if method == "DELETE":
                    assert not (expected / "note.txt").exists()
                else:
                    assert (expected / "note.txt").read_text() == "updated"
                changes = await client.get(f"{base}/changes")
                assert changes.status_code == 200, changes.text
                assert [item["path"] for item in changes.json()["data"]] == ["note.txt"]
                diff = await client.get(f"{base}/diff/note.txt")
                assert diff.status_code == 200, diff.text
                if method != "DELETE":
                    assert diff.json()["before"] == "project-original"
                assert diff.json()["after"] == (None if method == "DELETE" else "updated")
        finally:
            await registry.cleanup_session("cold-session")
