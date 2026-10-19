"""Windows process containment belongs to the session, not individual OS calls."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.spec.types import AgentSpec


class _ProcessEnvironment:
    def __init__(self, spec: OSEnvSpec) -> None:
        self.sandbox = SimpleNamespace(backend_type=spec.sandbox.type, copy_on_write_roots=())
        self.closed = False
        self.commands: list[str] = []

    async def shell(self, command: str, timeout: int | None = None) -> dict:
        del timeout
        assert not self.closed, "The session's process environment was closed between calls"
        self.commands.append(command)
        return {"stdout": command, "exit_code": 0}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def environments(monkeypatch: pytest.MonkeyPatch) -> list[_ProcessEnvironment]:
    created: list[_ProcessEnvironment] = []

    def create(spec: OSEnvSpec, **kwargs: object) -> _ProcessEnvironment:
        del kwargs
        environment = _ProcessEnvironment(spec)
        created.append(environment)
        return environment

    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", create)
    return created


def _spec(root: Path, sandbox: str = "windows_jobobject") -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process", cwd=str(root), sandbox=OSEnvSandboxSpec(type=sandbox)
        ),
    )


@pytest.mark.asyncio
async def test_windows_tools_keep_session_environment_until_cleanup(
    tmp_path: Path, environments: list[_ProcessEnvironment]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    spec = _spec(tmp_path)
    try:
        for command in ("launch-editor", "query-editor"):
            result = await _execute_os_env_tool(
                "sys_os_shell",
                {"command": command},
                agent_spec=spec,
                conversation_id="session-a",
                resource_registry=registry,
                runner_workspace=tmp_path,
            )
            assert json.loads(result)["exit_code"] == 0
            assert len(environments) == 1
            assert not environments[0].closed
        assert environments[0].commands == ["launch-editor", "query-editor"]
    finally:
        await registry.cleanup_session("session-a")
    assert environments[0].closed


@pytest.mark.asyncio
async def test_windows_sessions_do_not_share_process_lifetime(
    tmp_path: Path, environments: list[_ProcessEnvironment]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        for session in ("session-a", "session-b"):
            await _execute_os_env_tool(
                "sys_os_shell",
                {"command": "launch"},
                agent_spec=_spec(tmp_path),
                conversation_id=session,
                resource_registry=registry,
                runner_workspace=tmp_path,
            )
        assert len(environments) == 2
        await registry.cleanup_session("session-a")
        assert environments[0].closed
        assert not environments[1].closed
    finally:
        await registry.cleanup_session("session-b")
    assert environments[1].closed


@pytest.mark.asyncio
async def test_windows_tools_upgrade_filesystem_panel_read_view(
    tmp_path: Path, environments: list[_ProcessEnvironment]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID)
        await _execute_os_env_tool(
            "sys_os_shell",
            {"command": "launch"},
            agent_spec=_spec(tmp_path),
            conversation_id="session-a",
            resource_registry=registry,
            runner_workspace=tmp_path,
        )
        assert len(environments) == 2
        assert environments[0].closed
        assert not environments[1].closed
        assert environments[1].sandbox.backend_type == "windows_jobobject"
    finally:
        await registry.cleanup_session("session-a")


@pytest.mark.asyncio
async def test_windows_environment_rejects_policy_changes(
    tmp_path: Path, environments: list[_ProcessEnvironment]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID, _spec(tmp_path))
        with pytest.raises(ValueError, match="active Windows Job Object environment"):
            registry.resolve_environment(
                "session-a", DEFAULT_ENVIRONMENT_ID, _spec(tmp_path, "none")
            )
        assert not environments[0].closed
    finally:
        await registry.cleanup_session("session-a")


@pytest.mark.asyncio
async def test_windows_tools_require_session_owner(tmp_path: Path) -> None:
    result = await _execute_os_env_tool(
        "sys_os_shell", {"command": "launch"}, agent_spec=_spec(tmp_path)
    )
    assert "require a session resource registry" in json.loads(result)["error"]
