"""Tests for :mod:`omnigent.onboarding.sandboxes.microsandbox_remote`."""

from __future__ import annotations

import json

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes import microsandbox_remote as remote
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE, SandboxGoneError
from omnigent.onboarding.sandboxes.microsandbox_remote import (
    DEFAULT_TOKEN_ENV,
    RemoteMicrosandboxLauncher,
    sandbox_name,
)
from omnigent.server.managed_hosts import (
    MICROSANDBOX_REMOTE_MANAGED_TOKEN_TTL_S,
    parse_sandbox_config,
)

ENDPOINT = "http://sandbox-host:8740"


class FakeHost:
    """In-memory sandbox host API recording every request it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sandboxes: dict[str, str] = {}
        self.exec_response: dict[str, object] = {
            "exit_code": 0,
            "stdout": "ok\n",
            "stderr": "",
            "timed_out": False,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if parts == ["v1", "host"]:
            return httpx.Response(200, json={"cpus": 32})
        name = parts[2]
        action = parts[3] if len(parts) > 3 else None
        if request.method == "PUT":
            self.sandboxes[name] = "running"
            return httpx.Response(200, json={"name": name, "created": True, "status": "running"})
        if name not in self.sandboxes:
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(404, json={"detail": f"sandbox {name} not found"})
        if request.method == "GET":
            return httpx.Response(200, json={"name": name, "status": self.sandboxes[name]})
        if request.method == "DELETE":
            del self.sandboxes[name]
            return httpx.Response(204)
        if action == "start":
            self.sandboxes[name] = "running"
            return httpx.Response(200, json={"name": name, "status": "running"})
        if action == "touch":
            return httpx.Response(204 if self.sandboxes[name] == "running" else 409)
        if action == "exec":
            return httpx.Response(200, json=self.exec_response)
        return httpx.Response(400)


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def launcher(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> RemoteMicrosandboxLauncher:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "static-token")
    monkeypatch.setenv("GIT_TOKEN", "ghp_test")
    return RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT + "/",
        cpus=30,
        memory_mib=4096,
        max_memory_mib=16384,
        idle_timeout_s=900,
        env=["GIT_TOKEN", "UNSET_VAR"],
        workspace_path="/home/omnigent",
        workspace_quota_mib=51200,
        transport=httpx.MockTransport(host),
    )


def test_sandbox_names_are_valid_and_unique() -> None:
    first, second = sandbox_name("Managed_A1B2!"), sandbox_name("Managed_A1B2!")
    assert first != second
    assert first.startswith("managed-a1b2-")
    long = sandbox_name("x" * 200)
    assert len(long) <= 63
    assert sandbox_name("___").startswith("sandbox-")


def test_provision_creates_a_sized_sandbox_with_workspace_and_env(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    launcher.prepare()
    sandbox_id = launcher.provision("managed-a1b2c3d4")
    put = host.requests[-1]
    assert put.method == "PUT"
    assert put.url.path == f"/v1/sandboxes/{sandbox_id}"
    assert put.headers["Authorization"] == "Bearer static-token"
    assert json.loads(put.content) == {
        "image": DEFAULT_HOST_IMAGE,
        "cpus": 30,
        "memory_mib": 4096,
        "max_memory_mib": 16384,
        "idle_timeout_s": 900,
        "env": {"GIT_TOKEN": "ghp_test"},
        "workspace_path": "/home/omnigent",
        "workspace_quota_mib": 51200,
    }


def test_run_returns_output_and_raises_on_failure_when_checked(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    sandbox_id = launcher.provision("managed")
    result = launcher.run(sandbox_id, "echo ok")
    assert (result.returncode, result.stdout) == (0, "ok\n")
    assert json.loads(host.requests[-1].content)["command"] == "echo ok"
    host.exec_response = {"exit_code": 3, "stdout": "", "stderr": "boom", "timed_out": False}
    assert launcher.run(sandbox_id, "false", check=False).returncode == 3
    with pytest.raises(click.ClickException, match="boom"):
        launcher.run(sandbox_id, "false")
    host.exec_response = {
        "exit_code": None,
        "stdout": "",
        "stderr": "timed out",
        "timed_out": True,
    }
    assert launcher.run(sandbox_id, "sleep 999", check=False).returncode == -1


def test_lifecycle_maps_missing_sandboxes(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    sandbox_id = launcher.provision("managed")
    assert launcher.is_running(sandbox_id) is True
    assert launcher.keep_alive(sandbox_id) is True
    host.sandboxes[sandbox_id] = "stopped"
    assert launcher.is_running(sandbox_id) is False
    assert launcher.keep_alive(sandbox_id) is False
    launcher.resume(sandbox_id)
    assert host.sandboxes[sandbox_id] == "running"
    launcher.terminate(sandbox_id)
    launcher.terminate(sandbox_id)
    assert launcher.is_running(sandbox_id) is False
    with pytest.raises(SandboxGoneError):
        launcher.resume(sandbox_id)
    with pytest.raises(SandboxGoneError):
        launcher.run(sandbox_id, "true")


def test_requires_a_token(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    launcher = RemoteMicrosandboxLauncher(endpoint=ENDPOINT, transport=httpx.MockTransport(host))
    with pytest.raises(click.ClickException, match=DEFAULT_TOKEN_ENV):
        launcher.prepare()
    assert host.requests == []


def test_audience_uses_a_cached_google_id_token(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch_id_token(_request: object, audience: str) -> str:
        calls.append(audience)
        return "id-token"

    pytest.importorskip("google.oauth2.id_token")
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", fetch_id_token)
    monkeypatch.setattr(remote, "_credentials", {})
    launcher = RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT, audience="https://sandbox-host", transport=httpx.MockTransport(host)
    )
    launcher.prepare()
    launcher.prepare()
    assert calls == ["https://sandbox-host"]
    assert host.requests[-1].headers["Authorization"] == "Bearer id-token"


def test_parse_config_builds_the_remote_launcher() -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "microsandbox_remote",
            "server_url": "https://omnigent.example.com",
            "microsandbox_remote": {
                "endpoint": ENDPOINT,
                "token_env": "SHIM_TOKEN",
                "image": "registry.example/omnigent-host@sha256:abc",
                "env": ["GIT_TOKEN"],
                "cpus": 30,
                "memory_mib": 4096,
                "max_memory_mib": 16384,
                "idle_timeout_s": 0,
                "workspace_path": "/home/omnigent",
                "workspace_quota_mib": 51200,
            },
        }
    )
    assert cfg is not None
    cfg = cfg.default
    assert cfg.provider == "microsandbox_remote"
    assert cfg.managed_launch_supported is True
    assert cfg.token_ttl_s == MICROSANDBOX_REMOTE_MANAGED_TOKEN_TTL_S
    launcher = cfg.launcher_factory()
    assert isinstance(launcher, RemoteMicrosandboxLauncher)
    assert launcher.can_resume is True
    assert launcher.capabilities.resume_stopped is True
    assert launcher.capabilities.cli_bootstrap is False


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({}, "endpoint"),
        ({"endpoint": ENDPOINT, "bogus": 1}, "bogus"),
        ({"endpoint": ENDPOINT, "cpus": 0}, "cpus"),
        ({"endpoint": ENDPOINT, "idle_timeout_s": -1}, "idle_timeout_s"),
    ],
)
def test_parse_config_rejects_invalid_sections(section: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_sandbox_config(
            {
                "provider": "microsandbox_remote",
                "server_url": "https://omnigent.example.com",
                "microsandbox_remote": section,
            }
        )
