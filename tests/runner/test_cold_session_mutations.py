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


@pytest.mark.asyncio
async def test_mcp_rejects_unconfirmed_workspace_and_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "launch"
    selected = tmp_path / "selected"
    launch.mkdir()
    selected.mkdir()
    reads = 0
    captured: list[Path | None] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == "/v1/sessions/retry-session":
            reads += 1
            if reads == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"workspace": str(selected), "agent_id": "test"})
        return httpx.Response(200, json={})

    async def dispatch(*, runner_workspace: Path | None = None, **kwargs: object) -> str:
        captured.append(runner_workspace)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", dispatch)
    registry = SessionResourceRegistry(runner_workspace=launch, per_session_workspace=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata), base_url="http://server"
    ) as server:
        app = create_runner_app(
            server_client=server,
            resource_registry=registry,
            runner_workspace=launch,
            per_session_workspace=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                response = await client.post(
                    "/v1/sessions/retry-session/mcp/execute",
                    json={
                        "method": "tools/call",
                        "params": {"name": "sys_os_write", "arguments": {}},
                    },
                )
                assert response.status_code == 200
                assert "error" in response.json(), response.text
                assert captured == []
                response = await client.post(
                    "/v1/sessions/retry-session/mcp/execute",
                    json={
                        "method": "tools/call",
                        "params": {"name": "sys_os_write", "arguments": {}},
                    },
                )
                assert "error" not in response.json(), response.text
                assert captured == [selected]
                assert reads == 2
        finally:
            await registry.cleanup_session("retry-session")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_unconfirmed_workspace_rejects_mutation_before_environment_is_cached(
    tmp_path: Path, method: str
) -> None:
    launch = tmp_path / "launch"
    selected = tmp_path / "selected"
    launch.mkdir()
    selected.mkdir()
    (launch / "note.txt").write_text("runner-original")
    (selected / "note.txt").write_text("project-original")
    available = False

    def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/recovery-session" and available:
            return httpx.Response(200, json={"workspace": str(selected), "agent_id": "test"})
        return httpx.Response(503, json={})

    registry = SessionResourceRegistry(runner_workspace=launch, per_session_workspace=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata), base_url="http://server"
    ) as server:
        app = create_runner_app(
            server_client=server,
            resource_registry=registry,
            runner_workspace=launch,
            per_session_workspace=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                url = (
                    "/v1/sessions/recovery-session/resources/environments/"
                    f"{DEFAULT_ENVIRONMENT_ID}/filesystem/note.txt"
                )
                body = (
                    {"content": "updated"}
                    if method == "PUT"
                    else {"old_text": "project-original", "new_text": "updated"}
                )
                response = await client.request(
                    method, url, json=body if method != "DELETE" else None
                )
                assert response.status_code == 503, response.text
                assert (launch / "note.txt").read_text() == "runner-original"
                assert (selected / "note.txt").read_text() == "project-original"
                assert "recovery-session" not in registry._primary_envs
                available = True
                response = await client.request(
                    method, url, json=body if method != "DELETE" else None
                )
                assert response.status_code == 200, response.text
                assert (launch / "note.txt").read_text() == "runner-original"
                if method == "DELETE":
                    assert not (selected / "note.txt").exists()
                else:
                    assert (selected / "note.txt").read_text() == "updated"
        finally:
            await registry.cleanup_session("recovery-session")


@pytest.mark.asyncio
async def test_direct_stream_metadata_outage_fails_before_harness_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse

    available = False
    calls: list[Path | None] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/stream-retry":
            if not available:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"agent_id": "test", "workspace": str(tmp_path)})
        return httpx.Response(200, json={})

    async def dispatch(*, runner_workspace: Path | None = None, **kwargs: object) -> str:
        calls.append(runner_workspace)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.dispatch_tool_locally", dispatch)
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_retry"}}),
            _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "action_required",
                        "name": "sys_os_write",
                        "call_id": "write",
                        "arguments": "{}",
                    },
                }
            ),
            _sse({"type": "response.completed", "response": {"id": "resp_retry"}}),
        ]
    )
    registry = SessionResourceRegistry(runner_workspace=tmp_path, per_session_workspace=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata), base_url="http://server"
    ) as server:
        app = create_runner_app(
            process_manager=_FakeProcessManager(harness),
            server_client=server,
            resource_registry=registry,
            runner_workspace=tmp_path,
            per_session_workspace=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                body = {
                    "type": "message",
                    "role": "user",
                    "agent_id": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "write"}],
                }
                first = await client.post(
                    "/v1/sessions/stream-retry/events?stream=true", json=body
                )
                assert first.status_code == 200
                assert "response.failed" in first.text
                assert "workspace_metadata_unavailable" in first.text
                assert harness.posted_bodies == [], "No pending harness call may be started"
                assert calls == []
                available = True
                second = await client.post(
                    "/v1/sessions/stream-retry/events?stream=true", json=body
                )
                assert second.status_code == 200, second.text
                assert "response.completed" in second.text
                assert len(harness.posted_bodies) == 1, "Recovered turn must not remain buffered"
                assert calls == [tmp_path]
        finally:
            await registry.cleanup_session("stream-retry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint", ["background-turn", "direct-stream", "title", "legacy-init"]
)
async def test_cold_copy_on_write_entrypoint_confirms_workspace_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from omnigent.inner.datamodel import WritePathSpec
    from omnigent.spec.types import ExecutorSpec
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse

    launch = tmp_path / "launch"
    selected = tmp_path / "selected"
    launch.mkdir()
    selected.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="cow-test",
        os_env=OSEnvSpec(
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=[WritePathSpec(".", True)]),
        ),
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    registry = SessionResourceRegistry(runner_workspace=launch, per_session_workspace=False)
    roots: list[Path | None] = []
    dispatched: list[Path | None] = []

    def create(session_id: str, agent_spec: object, **kwargs: object):
        root = registry.workspace_for_session(session_id)
        roots.append(root)
        return Mock(
            cwd=root,
            sandbox=SimpleNamespace(
                backend_type="linux_bwrap",
                copy_on_write_roots=(root,),
                copy_on_write_namespace=(1, 2, 3),
            ),
        )

    monkeypatch.setattr(registry, "_create_primary_env", create)
    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_openai_agents_sdk_spawn_env", lambda spec: {}
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    async def dispatch(**kwargs: object) -> str:
        environment = registry.resolve_environment("cow-cold", DEFAULT_ENVIRONMENT_ID, spec)
        dispatched.append(environment.cwd)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.dispatch_tool_locally", dispatch)
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "cow-response"}}),
            _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "action_required",
                        "name": "sys_os_shell",
                        "call_id": "cow-call",
                        "arguments": '{"command":"echo ok"}',
                    },
                }
            ),
            _sse({"type": "response.completed", "response": {"id": "cow-response"}}),
        ]
    )

    available = entrypoint in {"background-turn", "direct-stream"}
    monkeypatch.setattr(
        "omnigent.runner.app.run_background_title", AsyncMock(return_value="Test title")
    )
    monkeypatch.setattr(
        "omnigent.runner.app.generator_spec_for_harness",
        lambda h: SimpleNamespace(resolver_harness=None),
    )

    def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/cow-cold":
            if not available:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"agent_id": "cow-agent", "workspace": str(selected)})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata), base_url="http://server"
    ) as server:
        app = create_runner_app(
            process_manager=_FakeProcessManager(harness),
            spec_resolver=resolve,
            server_client=server,
            resource_registry=registry,
            runner_workspace=launch,
            per_session_workspace=False,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:

                async def request() -> httpx.Response:
                    if entrypoint == "title":
                        return await client.post(
                            "/v1/sessions/cow-cold/background-title",
                            json={"agent_id": "cow-agent", "prompt": "test title"},
                        )
                    if entrypoint == "legacy-init":
                        return await client.post(
                            "/v1/sessions",
                            json={"session_id": "cow-cold", "agent_id": "cow-agent"},
                        )
                    return await client.post(
                        "/v1/sessions/cow-cold/events",
                        params={"stream": str(entrypoint == "direct-stream").lower()},
                        json={
                            "type": "message",
                            "role": "user",
                            "agent_id": "cow-agent",
                            "harness": "openai-agents",
                            "content": [{"type": "input_text", "text": "test"}],
                        },
                    )

                if not available:
                    failed = await request()
                    assert failed.status_code == 503, failed.text
                    assert roots == []
                    assert "cow-cold" not in registry._primary_envs
                    available = True
                response = await request()
                expected_status = {
                    "title": 200,
                    "legacy-init": 201,
                    "background-turn": 202,
                    "direct-stream": 200,
                }[entrypoint]
                assert response.status_code == expected_status, response.text
                if entrypoint in {"background-turn", "direct-stream"}:
                    async with asyncio.timeout(5):
                        while not dispatched:
                            await asyncio.sleep(0.01)
                else:
                    environment = registry.resolve_environment(
                        "cow-cold", DEFAULT_ENVIRONMENT_ID, spec
                    )
                    dispatched.append(environment.cwd)
                assert roots == [selected]
                assert dispatched == [selected]
        finally:
            await registry.cleanup_session("cow-cold")
