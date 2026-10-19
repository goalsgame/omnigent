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
        from omnigent.inner.sandbox import uses_windows_jobobject

        backend = "windows_jobobject" if uses_windows_jobobject(spec) else spec.sandbox.type
        self.sandbox = SimpleNamespace(backend_type=backend, copy_on_write_roots=())
        self.cwd = spec.cwd
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


@pytest.mark.asyncio
async def test_windows_dispatch_rejects_backend_downgrade(
    tmp_path: Path, environments: list[_ProcessEnvironment]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        for backend in ("windows_jobobject", "none"):
            result = json.loads(
                await _execute_os_env_tool(
                    "sys_os_shell",
                    {"command": backend},
                    agent_spec=_spec(tmp_path, backend),
                    conversation_id="session-a",
                    resource_registry=registry,
                    runner_workspace=tmp_path,
                )
            )
            if backend == "none":
                assert "active Windows Job Object environment" in result["error"]
        assert len(environments) == 1
        assert environments[0].commands == ["windows_jobobject"]
    finally:
        await registry.cleanup_session("session-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_kind", ["implicit-sandbox", "missing-os-env", "missing-agent"])
@pytest.mark.parametrize("read_view_first", [False, True])
async def test_implicit_windows_environment_is_owned_by_session(
    tmp_path: Path,
    environments: list[_ProcessEnvironment],
    monkeypatch: pytest.MonkeyPatch,
    agent_kind: str,
    read_view_first: bool,
) -> None:
    monkeypatch.setattr(
        "omnigent.inner.sandbox._default_sandbox_for_platform",
        lambda: OSEnvSandboxSpec(type="windows_jobobject"),
    )
    spec = (
        AgentSpec(spec_version=1, os_env=OSEnvSpec(cwd=str(tmp_path)))
        if agent_kind == "implicit-sandbox"
        else AgentSpec(spec_version=1)
        if agent_kind == "missing-os-env"
        else None
    )
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    try:
        if read_view_first:
            registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID)
        for command in ("launch", "inspect"):
            result = json.loads(
                await _execute_os_env_tool(
                    "sys_os_shell",
                    {"command": command},
                    agent_spec=spec,
                    conversation_id="session-a",
                    resource_registry=registry,
                    runner_workspace=tmp_path,
                )
            )
            assert result["exit_code"] == 0
            assert not environments[-1].closed
        assert len(environments) == (2 if read_view_first else 1)
        if read_view_first:
            assert environments[0].closed
        assert environments[-1].sandbox.backend_type == "windows_jobobject"
    finally:
        await registry.cleanup_session("session-a")
    assert environments[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [False, True])
async def test_selected_workspace_wins_for_execution_and_resource_views(
    tmp_path: Path, environments: list[_ProcessEnvironment], isolate: bool
) -> None:
    launch_root = tmp_path / "runner"
    selected_root = tmp_path / "project"
    selected_root.mkdir()
    registry = SessionResourceRegistry(runner_workspace=launch_root, per_session_workspace=isolate)
    registry.set_session_workspace_resolver(lambda session: selected_root)
    expected_root = selected_root / "session-a" if isolate else selected_root
    spec = _spec(launch_root)
    try:
        result = json.loads(
            await _execute_os_env_tool(
                "sys_os_shell",
                {"command": "launch"},
                agent_spec=spec,
                conversation_id="session-a",
                resource_registry=registry,
                runner_workspace=selected_root,
            )
        )
        assert result["exit_code"] == 0
        assert environments[0].cwd == str(expected_root)
        assert (
            registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID, spec)
            is environments[0]
        )
        assert registry.compute_default_env_root("session-a", spec) == str(expected_root)
        if isolate:
            second_root = registry.compute_default_env_root("session-b", spec)
            assert second_root == str(selected_root / "session-b")
            assert second_root != str(expected_root)
    finally:
        await registry.cleanup_session("session-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("tools_first", [True, False])
@pytest.mark.parametrize("cwd", [None, "."])
async def test_placeholder_workspace_matches_both_entry_points(
    tmp_path: Path,
    environments: list[_ProcessEnvironment],
    monkeypatch: pytest.MonkeyPatch,
    tools_first: bool,
    cwd: str | None,
) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_OS_ENV_ROOT", raising=False)
    monkeypatch.setattr("omnigent.runner.resource_registry._DEFAULT_WORKSPACE_ROOT", str(tmp_path))
    spec = _spec(tmp_path)
    spec.os_env.cwd = cwd
    registry = SessionResourceRegistry()
    try:
        if not tools_first:
            registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID, spec)
        result = json.loads(
            await _execute_os_env_tool(
                "sys_os_shell",
                {"command": "launch"},
                agent_spec=spec,
                conversation_id="session-a",
                resource_registry=registry,
            )
        )
        assert result["exit_code"] == 0
        assert (
            registry.resolve_environment("session-a", DEFAULT_ENVIRONMENT_ID, spec)
            is environments[0]
        )
        assert environments[0].cwd == str(tmp_path / "session-a" / "workspace")
        assert len(environments) == 1
    finally:
        await registry.cleanup_session("session-a")


@pytest.mark.asyncio
async def test_windows_file_reads_do_not_wait_for_running_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from omnigent.inner.os_env import CallerProcessOSEnvironment
    from omnigent.inner.sandbox import SandboxPolicy

    started = threading.Event()
    release = threading.Event()
    helpers = []

    class Helper:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False
            self.lock = threading.Lock()
            helpers.append(self)

        def request(self, request: dict) -> dict:
            with self.lock:
                if request["op"] == "shell" and request["command"] == "long-running-command":
                    started.set()
                    assert release.wait(5), "The shell was never released"
                    return {"exit_code": 0}
                if request["op"] == "shell":
                    return {
                        "stdout": '[{"n": "note.txt", "s": 2, "m": 0, "t": "f"}]',
                        "exit_code": 0,
                    }
                return {"content": "file-preview-ok"}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("omnigent.inner.os_env._HelperProcessClient", Helper)
    environment = CallerProcessOSEnvironment(
        spec=OSEnvSpec(),
        cwd=tmp_path,
        shell_path="cmd.exe",
        sandbox=SandboxPolicy(
            backend_type="windows_jobobject",
            active=True,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )
    shell = asyncio.create_task(environment.shell("long-running-command"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
        result = await asyncio.wait_for(environment.read("note.txt"), timeout=1)
        assert result["content"] == "file-preview-ok"
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        listing = await asyncio.wait_for(
            CallerProcessFilesystem(environment).list_dir(), timeout=1
        )
        assert listing.data[0].name == "note.txt"
        assert not shell.done()
    finally:
        release.set()
        await shell
        environment.close()
    assert len(helpers) == 2
    assert all(helper.closed for helper in helpers)


@pytest.mark.asyncio
async def test_selected_isolated_workspace_is_created_before_first_shell(tmp_path: Path) -> None:
    import sys

    registry = SessionResourceRegistry(
        runner_workspace=tmp_path / "runner", per_session_workspace=True
    )
    project = tmp_path / "project"
    registry.set_session_workspace_resolver(lambda session: project)
    spec = _spec(project, "none")
    try:
        environment = registry.resolve_environment("cold-session", DEFAULT_ENVIRONMENT_ID, spec)
        result = await environment.shell(f'"{sys.executable}" -c "print(123)"')
        assert result.get("exit_code") == 0, result
        assert result["stdout"].strip() == "123"
        assert Path(environment.cwd) == project / "cold-session"
    finally:
        await registry.cleanup_session("cold-session")


@pytest.mark.asyncio
async def test_windows_channels_share_scratch_for_relative_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import os_env
    from omnigent.inner.sandbox import SandboxPolicy

    class Helper:
        def __init__(self, *, shared_scratch: Path, **kwargs: object) -> None:
            self.root = shared_scratch
            self.closed = False

        def request(self, request: dict) -> dict:
            assert self.root.is_dir()
            if request["op"] == "shell":
                (self.root / "note.txt").write_text("scratch-ok")
                return {"exit_code": 0}
            return {"content": (self.root / request["path"]).read_text()}

        def close(self) -> None:
            if not self.closed:
                assert self.root.is_dir(), "Scratch removed before both helpers closed"
                self.closed = True

    monkeypatch.setattr(os_env, "_HelperProcessClient", Helper)
    environment = os_env.CallerProcessOSEnvironment(
        spec=OSEnvSpec(),
        cwd=tmp_path,
        shell_path="cmd.exe",
        _start_in_scratch=True,
        sandbox=SandboxPolicy(
            backend_type="windows_jobobject",
            active=True,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )
    scratch = environment._shared_scratch
    assert scratch is not None
    try:
        result = await environment.shell("printf scratch-ok > note.txt")
        assert result.get("exit_code") == 0, result
        assert (await environment.read("note.txt"))["content"] == "scratch-ok"
    finally:
        environment.close()
    assert not scratch.exists()


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_native_windows_scratch_shell_to_file_round_trip(tmp_path: Path) -> None:
    from omnigent.inner.os_env import create_os_environment

    environment = create_os_environment(
        OSEnvSpec(
            cwd=str(tmp_path),
            start_in_scratch=True,
            sandbox=OSEnvSandboxSpec(type="windows_jobobject"),
        )
    )
    assert environment is not None
    try:
        result = await environment.shell("echo scratch-ok>note.txt")
        assert result.get("exit_code") == 0, result
        assert (await environment.read("note.txt"))["content"].strip() == "scratch-ok"
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_session_cleanup_interrupts_blocked_windows_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import io
    import threading

    from omnigent.inner.os_env import CallerProcessOSEnvironment
    from omnigent.inner.sandbox import SandboxPolicy

    started, released = threading.Event(), threading.Event()
    starts = []

    class Output:
        def readline(self) -> str:
            started.set()
            assert released.wait(5), "Session cleanup did not interrupt the shell"
            return ""

        def close(self) -> None:
            pass

    class Process:
        pid = None
        stdin = io.StringIO()
        stdout = Output()
        stderr = io.StringIO()

        def poll(self) -> int | None:
            return 0 if released.is_set() else None

    class Handle:
        def close(self) -> None:
            released.set()

    environment = CallerProcessOSEnvironment(
        spec=OSEnvSpec(),
        cwd=tmp_path,
        shell_path="cmd.exe",
        sandbox=SandboxPolicy(
            backend_type="windows_jobobject",
            active=False,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )
    helper = environment._shell_helper
    helper._proc = Process()
    helper._sandbox_handle = Handle()
    monkeypatch.setattr(helper, "_ensure_started_locked", lambda: starts.append(True))
    registry = SessionResourceRegistry()
    registry._primary_envs["session-a"] = environment
    command = asyncio.create_task(environment.shell("blocked"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3)
        await asyncio.wait_for(registry.cleanup_session("session-a"), 2)
        result = await asyncio.wait_for(command, 2)
        assert "error" in result
        assert starts == [True], "Interrupted commands must not be retried"
    finally:
        released.set()
        await command
        environment.close()


@pytest.mark.asyncio
async def test_windows_command_cancellation_allows_next_call_and_preserves_prior_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    import psutil

    from omnigent.inner.os_env import CallerProcessOSEnvironment, _run_windows_shell
    from omnigent.inner.sandbox import SandboxPolicy

    environment = CallerProcessOSEnvironment(
        spec=OSEnvSpec(),
        cwd=tmp_path,
        shell_path=sys.executable,
        sandbox=SandboxPolicy(
            backend_type="windows_jobobject",
            active=False,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )
    helper = environment._shell_helper

    def request(payload: dict) -> dict:
        with helper._lock:
            try:
                result = _run_windows_shell(
                    [sys.executable, "-c", payload["command"]],
                    cwd=tmp_path,
                    timeout=60,
                    cancel_path=Path(payload["cancel_path"]),
                )
                return {"stdout": result.stdout, "exit_code": result.returncode}
            except InterruptedError:
                return {"error": "cancelled"}

    monkeypatch.setattr(helper, "request", request)
    earlier = None
    active = None
    try:
        result = await environment.shell(
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)"
        )
        earlier = psutil.Process(int(result["stdout"].strip()))
        marker = tmp_path / "active.pid"
        active = asyncio.create_task(
            environment.shell(
                "import os,time; from pathlib import Path; "
                f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
            )
        )
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        active_pid = int(marker.read_text())
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, 3)
        result = await asyncio.wait_for(environment.shell("print('next-call-ok')"), 3)
        assert result["stdout"].strip() == "next-call-ok"
        assert earlier.is_running() and earlier.status() != psutil.STATUS_ZOMBIE
        assert (
            not psutil.pid_exists(active_pid)
            or psutil.Process(active_pid).status() == psutil.STATUS_ZOMBIE
        )
        assert not list(environment._command_control_dir.iterdir())
    finally:
        if active is not None and not active.done():
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        environment.close()
        if earlier is not None:
            try:
                earlier.kill()
                earlier.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
